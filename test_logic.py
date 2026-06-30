"""Tests for logic.py — run with: pytest test_logic.py"""
import pytest
from logic import (
    trend,
    relative_humidity,
    heat_index_f,
    wind_chill_f,
    condition_statuses,
    composite_score,
    status_label,
)

# Default config matching the original hard-coded constants
_CFG = {
    "good_mph":           15.0,
    "caution_mph":        23.0,
    "gauge_max":          30.0,
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
}


def _state(**kwargs):
    base = {
        "wind": None, "gust": None, "wdir": "---",
        "wtmp": None, "wvht": None, "atmp": None, "dpd": None,
        "dewp": None, "pres": None,
        "pres_history": [], "gust_history": [], "alerts": [],
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# trend()
# ---------------------------------------------------------------------------

def test_trend_up():
    assert trend([18.0, 16.0, 14.0, 14.0]) == "up"


def test_trend_down():
    assert trend([10.0, 12.0, 14.0, 14.0]) == "down"


def test_trend_steady():
    assert trend([14.0, 14.5, 14.0, 14.0]) == "steady"


def test_trend_insufficient_history():
    assert trend([]) is None
    assert trend([10.0, 11.0]) is None
    assert trend([10.0, 11.0, 12.0]) is None


def test_trend_exactly_four_entries():
    assert trend([14.0, 13.0, 12.0, 11.0]) == "up"


# ---------------------------------------------------------------------------
# relative_humidity()
# ---------------------------------------------------------------------------

def test_rh_saturated():
    """When air temp equals dewpoint, RH should be ~100%."""
    rh = relative_humidity(70.0, 70.0)
    assert abs(rh - 100.0) < 0.1


def test_rh_dry():
    rh = relative_humidity(90.0, 32.0)
    assert rh < 15.0


def test_rh_clamped_low():
    # Physically impossible (dewpoint far above air temp) → clamped to 100%
    assert relative_humidity(32.0, 90.0) == 100.0


# ---------------------------------------------------------------------------
# heat_index_f()
# ---------------------------------------------------------------------------

def test_heat_index_known_value():
    # At 95°F / 50% RH the NWS formula gives ~104.7°F
    hi = heat_index_f(95.0, 50.0)
    assert 103.0 < hi < 107.0


def test_heat_index_exact_at_80_40():
    # NWS reference: 80°F / 40% → ~79.6°F (barely registers)
    hi = heat_index_f(80.0, 40.0)
    assert 78.0 < hi < 82.0


# ---------------------------------------------------------------------------
# wind_chill_f()
# ---------------------------------------------------------------------------

def test_wind_chill_known_value():
    # NOAA reference: 30°F air, 10 mph wind → ~21°F wind chill
    wc = wind_chill_f(30.0, 10.0)
    assert 18.0 < wc < 24.0


def test_wind_chill_no_wind():
    # At very low wind speed the formula still runs; just smoke-test it
    wc = wind_chill_f(20.0, 3.0)
    assert wc < 20.0


# ---------------------------------------------------------------------------
# condition_statuses()
# ---------------------------------------------------------------------------

def test_wind_status_go():
    ws, wvs, ts = condition_statuses(_state(gust=10.0), _CFG)
    assert ws == "GO"


def test_wind_status_caution():
    ws, _, _ = condition_statuses(_state(gust=18.0), _CFG)
    assert ws == "CAUTION"


def test_wind_status_nogo():
    ws, _, _ = condition_statuses(_state(gust=25.0), _CFG)
    assert ws == "NO-GO"


def test_wave_status_go():
    _, wvs, _ = condition_statuses(_state(wvht=1.5), _CFG)
    assert wvs == "GO"


def test_wave_status_caution():
    _, wvs, _ = condition_statuses(_state(wvht=2.5), _CFG)
    assert wvs == "CAUTION"


def test_wave_status_nogo():
    _, wvs, _ = condition_statuses(_state(wvht=3.5), _CFG)
    assert wvs == "NO-GO"


def test_wave_chop_upgrades_go_to_caution():
    # 1.5 ft waves with 4s DPD (short chop) → CAUTION even though height is GO
    _, wvs, _ = condition_statuses(_state(wvht=1.5, dpd=4.0), _CFG)
    assert wvs == "CAUTION"


def test_wave_chop_does_not_upgrade_when_height_below_1ft():
    _, wvs, _ = condition_statuses(_state(wvht=0.5, dpd=4.0), _CFG)
    assert wvs == "GO"


def test_temp_status_go():
    _, _, ts = condition_statuses(_state(wtmp=70.0), _CFG)
    assert ts == "GO"


def test_temp_status_caution():
    _, _, ts = condition_statuses(_state(wtmp=57.0), _CFG)
    assert ts == "CAUTION"


def test_temp_status_nogo():
    _, _, ts = condition_statuses(_state(wtmp=45.0), _CFG)
    assert ts == "NO-GO"


def test_all_none_returns_go():
    ws, wvs, ts = condition_statuses(_state(), _CFG)
    assert ws == "GO" and wvs == "GO" and ts == "GO"


# ---------------------------------------------------------------------------
# composite_score() and status_label()
# ---------------------------------------------------------------------------

def test_calm_conditions_are_go():
    s = _state(gust=8.0, wind=6.0, wvht=1.0, wtmp=72.0)
    score = composite_score(s, _CFG)
    assert status_label(score, _CFG) == "GO"
    assert score < _CFG["good_mph"]


def test_high_wind_is_nogo():
    s = _state(gust=28.0, wind=25.0)
    score = composite_score(s, _CFG)
    assert status_label(score, _CFG) == "NO-GO"


def test_moderate_wind_is_caution():
    s = _state(gust=18.0, wind=16.0)
    score = composite_score(s, _CFG)
    assert status_label(score, _CFG) == "CAUTION"


def test_moderate_advisory_does_not_change_verdict():
    """A Moderate advisory (e.g. Small Craft Advisory) is informational only —
    light wind stays GO; the real wind/wave readings drive any caution."""
    s = _state(gust=5.0, wind=4.0, wtmp=72.0,
               alerts=[("SMALL CRAFT ADVISORY", "Moderate")])
    score = composite_score(s, _CFG)
    assert status_label(score, _CFG) == "GO"


def test_severe_alert_forces_nogo_floor():
    """Severe/Extreme alerts (the genuinely dangerous ones) floor at NO-GO."""
    s = _state(gust=5.0, wind=4.0, alerts=[("GALE WARNING", "Severe")])
    score = composite_score(s, _CFG)
    assert status_label(score, _CFG) == "NO-GO"


def test_extreme_alert_forces_nogo_floor():
    s = _state(gust=5.0, wind=4.0, alerts=[("HURRICANE WARNING", "Extreme")])
    score = composite_score(s, _CFG)
    assert status_label(score, _CFG) == "NO-GO"


def test_rip_current_statement_does_not_force_caution():
    """Perpetual coastal swimmer advisories must not peg the gauge yellow on calm days."""
    s = _state(gust=5.0, wind=4.0, wtmp=72.0,
               alerts=[("RIP CURRENT STATEMENT", "Moderate")])
    score = composite_score(s, _CFG)
    assert status_label(score, _CFG) == "GO"


def test_minor_alert_does_not_force_caution():
    """Minor/Unknown-severity advisories are informational only."""
    s = _state(gust=5.0, wind=4.0, wtmp=72.0,
               alerts=[("COASTAL FLOOD ADVISORY", "Minor")])
    score = composite_score(s, _CFG)
    assert status_label(score, _CFG) == "GO"


def test_severe_non_boating_alert_does_not_force_nogo():
    """A Severe land/heat warning must not force a boating NO-GO on calm wind;
    the heat-index term handles on-deck heat separately."""
    s = _state(gust=5.0, wind=4.0, wtmp=72.0,
               alerts=[("EXCESSIVE HEAT WARNING", "Severe")])
    score = composite_score(s, _CFG)
    assert status_label(score, _CFG) == "GO"


def test_fog_forces_caution():
    s = _state(gust=5.0, atmp=62.0, dewp=61.0)  # spread = 1 < 3 → fog
    score = composite_score(s, _CFG)
    assert score >= _CFG["good_mph"]


def test_no_fog_on_merely_humid_day():
    s = _state(gust=5.0, atmp=62.0, dewp=58.0)  # spread = 4 > 3 → humid, not fog
    score = composite_score(s, _CFG)
    assert score < _CFG["good_mph"]


def test_no_fog_when_spread_sufficient():
    s = _state(gust=5.0, atmp=75.0, dewp=60.0)  # spread = 15 > 3 → no fog penalty
    score = composite_score(s, _CFG)
    assert score < _CFG["good_mph"]


def test_falling_pressure_adds_caution_floor():
    # pres_history is most-recent-first: pressure dropped from 1015 to 1012 (3 hPa fall)
    pres_history = [1012.0, 1013.0, 1014.0, 1015.0]
    s = _state(gust=5.0, pres=1012.0, pres_history=pres_history)
    score = composite_score(s, _CFG)
    assert score >= _CFG["good_mph"]


def test_stable_pressure_no_penalty():
    # pres_history most-recent-first: pressure rising slightly — no penalty
    pres_history = [1014.5, 1014.0, 1013.5, 1013.0]
    s = _state(gust=5.0, pres=1014.5, pres_history=pres_history)
    score = composite_score(s, _CFG)
    assert score < _CFG["good_mph"]


def test_dangerous_heat_index_forces_caution():
    """A high heat index (NWS 'Danger') must read at least CAUTION on calm wind."""
    s = _state(gust=6.0, wind=4.0, atmp=95.0, dewp=78.0)  # heat index ~110°F
    score = composite_score(s, _CFG)
    assert status_label(score, _CFG) in ("CAUTION", "NO-GO")
    assert score >= _CFG["good_mph"]


def test_extreme_heat_index_forces_nogo():
    """An 'Extreme Danger' heat index (~125°F+) should read NO-GO."""
    s = _state(gust=6.0, wind=4.0, atmp=104.0, dewp=84.0)
    score = composite_score(s, _CFG)
    assert status_label(score, _CFG) == "NO-GO"


def test_dry_heat_still_gets_relief_not_penalty():
    """Hot but dry air (low humidity → modest heat index) stays GO on light wind."""
    s = _state(gust=8.0, wind=6.0, atmp=95.0, dewp=50.0)  # ~20% RH
    score = composite_score(s, _CFG)
    assert status_label(score, _CFG) == "GO"


def test_wind_chill_strengthens_cold_penalty():
    """A cold, windy day scores no lower than the same temperature with calm air."""
    windy = _state(gust=8.0, wind=12.0, atmp=40.0)
    calm  = _state(gust=8.0, wind=0.0,  atmp=40.0)
    assert composite_score(windy, _CFG) >= composite_score(calm, _CFG)


def test_warm_air_reduces_wind_eq():
    """Hot day should give slight relief compared to cold day at same gust."""
    hot  = _state(gust=16.0, atmp=95.0)
    cold = _state(gust=16.0, atmp=60.0)
    assert composite_score(hot, _CFG) < composite_score(cold, _CFG)


def test_big_waves_force_nogo_on_calm_wind():
    """A large sea must read NO-GO even when the wind is dead calm."""
    for wv in (5.0, 8.0, 20.0):
        s = _state(gust=3.0, wind=2.0, wvht=wv, dpd=7.0)
        score = composite_score(s, _CFG)
        assert status_label(score, _CFG) == "NO-GO", f"{wv} ft should be NO-GO"


def test_moderate_waves_are_caution_not_nogo():
    """~4 ft seas on calm wind stay CAUTION, not NO-GO."""
    s = _state(gust=3.0, wind=2.0, wvht=4.0, dpd=7.0)
    score = composite_score(s, _CFG)
    assert status_label(score, _CFG) == "CAUTION"


def test_chop_increases_wave_score():
    choppy  = _state(gust=5.0, wvht=1.5, dpd=4.0)
    smooth  = _state(gust=5.0, wvht=1.5, dpd=10.0)
    assert composite_score(choppy, _CFG) > composite_score(smooth, _CFG)


def test_score_capped_at_gauge_max():
    s = _state(gust=100.0)
    assert composite_score(s, _CFG) == _CFG["gauge_max"]


def test_score_non_negative():
    s = _state(gust=0.0)
    assert composite_score(s, _CFG) >= 0.0


def test_custom_thresholds_respected():
    cfg_custom = dict(_CFG, good_mph=10.0, caution_mph=18.0)
    s = _state(gust=12.0)
    score = composite_score(s, cfg_custom)
    # 12 mph is GO in default config but CAUTION with good_mph=10
    assert status_label(score, cfg_custom) == "CAUTION"
    assert status_label(score, _CFG)       == "GO"


# ---------------------------------------------------------------------------
# status_label()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score,expected", [
    (0.0,  "GO"),
    (14.9, "GO"),
    (15.0, "CAUTION"),
    (23.0, "CAUTION"),
    (23.1, "NO-GO"),
    (30.0, "NO-GO"),
])
def test_status_label_boundaries(score, expected):
    assert status_label(score, _CFG) == expected
