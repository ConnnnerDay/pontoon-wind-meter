"""
Go/No-Go scoring and weather calculations.

All functions are pure (no I/O, no hardware dependencies) and accept an
explicit ``cfg`` dict so they can be unit-tested without touching any
global state.
"""

from __future__ import annotations

import math


# ---------------------------------------------------------------------------
# Gust trend
# ---------------------------------------------------------------------------

def trend(history: list[float]) -> str | None:
    """Return ``'up'``, ``'down'``, or ``'steady'`` from a most-recent-first
    gust list, or ``None`` when there is not enough history."""
    if len(history) < 4:
        return None
    delta = history[0] - history[3]   # newest vs ~15 min ago
    if delta >  1.5:
        return "up"
    if delta < -1.5:
        return "down"
    return "steady"


# ---------------------------------------------------------------------------
# Weather comfort calculations
# ---------------------------------------------------------------------------

def relative_humidity(atmp_f: float, dewp_f: float) -> float:
    """Approximate relative humidity (%) from air temp and dewpoint (°F)."""
    tc = (atmp_f - 32) * 5 / 9
    td = (dewp_f - 32) * 5 / 9
    return max(0.0, min(100.0,
        100.0 * math.exp(17.625 * td / (243.04 + td))
              / math.exp(17.625 * tc / (243.04 + tc))
    ))


def heat_index_f(t_f: float, rh: float) -> float:
    """Rothfusz heat index (°F). Only meaningful when t_f ≥ 80 and rh ≥ 40."""
    return (
        -42.379
        + 2.04901523  * t_f
        + 10.14333127 * rh
        - 0.22475541  * t_f * rh
        - 0.00683783  * t_f ** 2
        - 0.05481717  * rh  ** 2
        + 0.00122874  * t_f ** 2 * rh
        + 0.00085282  * t_f      * rh ** 2
        - 0.00000199  * t_f ** 2 * rh ** 2
    )


def wind_chill_f(t_f: float, wind_mph: float) -> float:
    """NOAA wind chill (°F). Only meaningful when t_f ≤ 50 and wind_mph ≥ 3."""
    return (
        35.74
        + 0.6215  * t_f
        - 35.75   * (wind_mph ** 0.16)
        + 0.4275  * t_f * (wind_mph ** 0.16)
    )


# ---------------------------------------------------------------------------
# Condition dots — per-factor status
# ---------------------------------------------------------------------------

def condition_statuses(state: dict, cfg: dict) -> tuple[str, str, str]:
    """Return ``(wind_status, wave_status, temp_status)`` each as
    ``'GO'`` / ``'CAUTION'`` / ``'NO-GO'``."""
    gust = state.get("gust") or 0.0
    wvht = state.get("wvht")
    wtmp = state.get("wtmp")
    dpd  = state.get("dpd")

    good_mph        = cfg["good_mph"]
    caution_mph     = cfg["caution_mph"]
    wave_good_ft    = cfg["wave_good_ft"]
    wave_caution_ft = cfg["wave_caution_ft"]
    wave_chop_dpd   = cfg["wave_chop_dpd"]
    temp_cool_f     = cfg["temp_cool_f"]
    temp_cold_f     = cfg["temp_cold_f"]

    # Wind
    if gust < good_mph:
        ws = "GO"
    elif gust <= caution_mph:
        ws = "CAUTION"
    else:
        ws = "NO-GO"

    # Waves
    if wvht is None or wvht < wave_good_ft:
        wvs = "GO"
    elif wvht <= wave_caution_ft:
        wvs = "CAUTION"
    else:
        wvs = "NO-GO"

    # Short-period chop upgrades borderline GO to CAUTION even at modest heights
    if wvs == "GO" and dpd is not None and dpd < wave_chop_dpd and wvht is not None and wvht >= 1.0:
        wvs = "CAUTION"

    # Water temperature
    if wtmp is None or wtmp >= temp_cool_f:
        ts = "GO"
    elif wtmp >= temp_cold_f:
        ts = "CAUTION"
    else:
        ts = "NO-GO"

    return ws, wvs, ts


# ---------------------------------------------------------------------------
# NOAA alert handling
# ---------------------------------------------------------------------------

# Land/swimmer/heat advisories that are common on the coast but say nothing
# about whether the wind is safe for a pontoon on the water.  They are still
# shown on the dashboard — they just don't drive the go/no-go verdict.
_NON_BOATING_EVENTS = frozenset({
    "RIP CURRENT STATEMENT",
    "BEACH HAZARDS STATEMENT",
    "AIR QUALITY ALERT",
    "HEAT ADVISORY",
    "EXCESSIVE HEAT WARNING",
    "EXCESSIVE HEAT WATCH",
})

# NOAA CAP severity levels, ranked.  "Minor"/"Unknown" advisories are
# informational and do not move the verdict on their own.
_SEVERITY_RANK = {"extreme": 4, "severe": 3, "moderate": 2, "minor": 1, "unknown": 0}


def alert_floor(alerts: list, cfg: dict) -> float:
    """Composite-score floor (mph-equivalent) contributed by active alerts.

    Rather than forcing CAUTION for *any* alert — which keeps the gauge pegged
    yellow on calm days thanks to perpetual coastal rip-current/beach
    statements — only boating-relevant alerts at Moderate severity or above
    count.  Moderate alerts floor at CAUTION; Severe/Extreme floor at NO-GO.
    Each ``alerts`` entry is an ``(event, severity)`` pair.
    """
    good_mph    = cfg["good_mph"]
    caution_mph = cfg["caution_mph"]
    floor = 0.0
    for event, severity in alerts:
        if (event or "").upper() in _NON_BOATING_EVENTS:
            continue
        rank = _SEVERITY_RANK.get((severity or "unknown").strip().lower(), 0)
        if rank >= 3:        # Severe / Extreme
            floor = max(floor, caution_mph + 0.5)
        elif rank == 2:      # Moderate
            floor = max(floor, good_mph + 0.5)
    return floor


# ---------------------------------------------------------------------------
# Composite go/no-go score
# ---------------------------------------------------------------------------

def composite_score(state: dict, cfg: dict) -> float:
    """Weighted go/no-go score on the 0–gauge_max scale.

    Wind gust is primary (full weight).  Wave height, water temperature,
    barometric pressure trend, and fog each contribute with lighter weights.
    A dangerous heat index or wind chill on the exposed deck, and any active
    NOAA alert, force the result to at least the CAUTION boundary.
    """
    gust         = state.get("gust") or 0.0
    wind         = state.get("wind")
    wvht         = state.get("wvht")
    wtmp         = state.get("wtmp")
    atmp         = state.get("atmp")
    alerts       = state.get("alerts") or []
    dpd          = state.get("dpd")
    dewp         = state.get("dewp")
    pres_history = state.get("pres_history") or []

    good_mph          = cfg["good_mph"]
    caution_mph       = cfg["caution_mph"]
    gauge_max         = cfg["gauge_max"]
    wave_good_ft      = cfg["wave_good_ft"]
    wave_caution_ft   = cfg["wave_caution_ft"]
    wave_chop_dpd     = cfg["wave_chop_dpd"]
    wave_swell_dpd    = cfg["wave_swell_dpd"]
    temp_cool_f       = cfg["temp_cool_f"]
    temp_cold_f       = cfg["temp_cold_f"]
    atmp_warm_f       = cfg["atmp_warm_f"]
    atmp_chilly_f     = cfg["atmp_chilly_f"]
    fog_spread_f      = cfg["fog_spread_f"]
    pres_fall_caution = cfg["pres_fall_caution"]

    wind_eq = min(float(gust), float(gauge_max))

    # Hot-day relief: warm air makes moderate wind feel refreshing.
    # Max 5 mph equivalent so dangerous winds can't disappear.
    if atmp is not None and atmp > atmp_warm_f:
        warm_relief = min(5.0, (atmp - atmp_warm_f) / 20.0 * 5.0)
        wind_eq = max(0.0, wind_eq - warm_relief)

    # Map wave height onto the mph scale
    if wvht is None or wvht <= 0:
        wave_eq = 0.0
    elif wvht <= wave_good_ft:
        wave_eq = (wvht / wave_good_ft) * good_mph
    elif wvht <= wave_caution_ft:
        t = (wvht - wave_good_ft) / (wave_caution_ft - wave_good_ft)
        wave_eq = good_mph + t * (caution_mph - good_mph)
    else:
        # Above the caution height, climb fast enough that the *weighted*
        # contribution (wave_eq * 0.75 below) crosses the NO-GO line just past
        # ~4.5 ft.  Do NOT clamp to gauge_max here — that pre-weight cap used to
        # hold the weighted result at 22.5 (CAUTION) no matter how big the sea,
        # so a 10 ft swell on calm wind never read NO-GO.  The final
        # ``min(result, gauge_max)`` is the real ceiling.
        wave_eq = caution_mph + (wvht - wave_caution_ft) * 5.0

    # DPD modulates wave danger: choppy short-period seas are worse on a pontoon
    if dpd is not None and wvht and wvht > 0:
        if dpd < wave_chop_dpd:
            wave_eq *= 1.20
        elif dpd >= wave_swell_dpd:
            wave_eq *= 0.85

    # On an exposed deck a cold breeze bites harder than the bare air temp, so
    # use the wind-chill "feels like" value when it is colder. (The NOAA chill
    # formula is only meaningful for atmp ≤ 50 °F and wind ≥ 3 mph.)
    feels_cold = atmp
    if atmp is not None and wind is not None and atmp <= 50 and wind >= 3:
        feels_cold = min(atmp, wind_chill_f(atmp, wind))

    # Temperature penalty (water + air, the latter via the feels-like value)
    temp_eq = 0.0
    if wtmp is not None and wtmp < temp_cool_f:
        span = max(1, temp_cool_f - temp_cold_f)
        temp_eq = max(temp_eq, min(caution_mph, (temp_cool_f - wtmp) / span * caution_mph))
    if feels_cold is not None:
        if feels_cold < temp_cold_f:
            temp_eq = max(temp_eq, min(good_mph, (temp_cold_f - feels_cold) / 15.0 * good_mph))
        elif feels_cold < atmp_chilly_f:
            chilly_frac = (atmp_chilly_f - feels_cold) / (atmp_chilly_f - temp_cold_f)
            temp_eq = max(temp_eq, chilly_frac * good_mph * 0.75)

    # Heat-index danger: an open deck offers no shade, so a high heat index is a
    # real hazard — and it must counterbalance the warm-wind relief above, which
    # otherwise nudges a brutally hot day toward GO. NWS "Danger" begins near a
    # 103 °F heat index; "Extreme Danger" near 125 °F.
    heat_eq = 0.0
    if atmp is not None and dewp is not None and atmp >= 80:
        rh = relative_humidity(atmp, dewp)
        if rh >= 40:
            hi = heat_index_f(atmp, rh)
            if hi >= 103:
                frac = min(1.0, max(0.0, (hi - 103) / (125 - 103)))
                heat_eq = (good_mph + 0.5) + frac * (caution_mph - good_mph)

    # Fog: tight air-dewpoint spread signals near-saturated air over the water
    fog_eq = 0.0
    if atmp is not None and dewp is not None and (atmp - dewp) < fog_spread_f:
        fog_eq = good_mph + 0.5   # always floors at CAUTION

    # Falling pressure signals incoming weather
    pres_eq = 0.0
    if len(pres_history) >= 3:
        oldest_p = next((p for p in reversed(pres_history) if p is not None), None)
        newest_p = pres_history[0] if pres_history[0] is not None else None
        if oldest_p is not None and newest_p is not None:
            pres_fall = oldest_p - newest_p
            if pres_fall >= pres_fall_caution:
                pres_eq = min(good_mph + 0.5, good_mph * (pres_fall / pres_fall_caution))

    result = max(
        wind_eq,
        wave_eq * 0.75,   # waves: ~3 ft → CAUTION; >4.5 ft → NO-GO
        temp_eq * 0.80,   # temp: marginal reading alone won't force NO-GO
        heat_eq,          # heat index: ≥103°F → CAUTION; ≥125°F → NO-GO
        pres_eq,
        fog_eq,
    )

    result = max(result, alert_floor(alerts, cfg))

    return min(result, gauge_max)


def status_label(score: float, cfg: dict) -> str:
    """Convert a composite score to a ``'GO'`` / ``'CAUTION'`` / ``'NO-GO'`` string."""
    if score < cfg["good_mph"]:
        return "GO"
    if score <= cfg["caution_mph"]:
        return "CAUTION"
    return "NO-GO"
