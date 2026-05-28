"""Tests for ndbc.py pure functions — run with: pytest test_ndbc.py"""
import pytest
from datetime import datetime, timezone, timedelta

from ndbc import ms_to_mph, celsius_to_f, m_to_ft, wind_direction, parse_ndbc, obs_age_minutes

# Minimal realistic NDBC realtime2 sample
SAMPLE = """\
#YY  MM DD hh mm WDIR WSPD  GST  WVHT
#yr  mo dy hr mn  deg m/s  m/s     m
24 05 28 12 00  210  5.1  6.2    MM
"""

SAMPLE_WITH_PREAMBLE = "#Advisory: high winds expected today\n" + SAMPLE

SAMPLE_MISSING_WIND = """\
#YY  MM DD hh mm WDIR WSPD  GST
#yr  mo dy hr mn  deg m/s  m/s
24 05 28 12 00   MM   MM   MM
"""


# --- ms_to_mph ---

def test_ms_to_mph_unit():
    assert abs(ms_to_mph(1.0) - 2.23694) < 1e-9


def test_ms_to_mph_zero():
    assert ms_to_mph(0) == 0


def test_ms_to_mph_scale():
    # 10 m/s should be ~22.37 mph
    assert abs(ms_to_mph(10.0) - 22.3694) < 1e-6


# --- celsius_to_f ---

def test_celsius_to_f_freezing():
    assert celsius_to_f(0) == 32.0


def test_celsius_to_f_boiling():
    assert abs(celsius_to_f(100) - 212.0) < 1e-9


def test_celsius_to_f_body_temp():
    assert abs(celsius_to_f(37) - 98.6) < 0.01


def test_celsius_to_f_negative():
    assert celsius_to_f(-40) == -40.0   # -40 is the crossover


# --- m_to_ft ---

def test_m_to_ft_unit():
    assert abs(m_to_ft(1.0) - 3.28084) < 1e-9


def test_m_to_ft_zero():
    assert m_to_ft(0) == 0


def test_m_to_ft_scale():
    # 10 m ≈ 32.8 ft
    assert abs(m_to_ft(10.0) - 32.8084) < 1e-6


# --- wind_direction ---

@pytest.mark.parametrize("deg,expected", [
    ("0",   "N"),
    ("45",  "NE"),
    ("90",  "E"),
    ("135", "SE"),
    ("180", "S"),
    ("225", "SW"),
    ("270", "W"),
    ("315", "NW"),
    ("360", "N"),
    ("23",  "NE"),   # 23/45 = 0.51 → rounds to 1
    ("337", "NW"),
])
def test_wind_direction_values(deg, expected):
    assert wind_direction(deg) == expected


def test_wind_direction_missing():
    assert wind_direction("MM") == "---"


def test_wind_direction_calm():
    assert wind_direction("999") == "---"


# --- parse_ndbc ---

def test_parse_ndbc_fields():
    row = parse_ndbc(SAMPLE)
    assert row["WSPD"] == "5.1"
    assert row["GST"] == "6.2"
    assert row["WDIR"] == "210"
    assert row["WVHT"] == "MM"


def test_parse_ndbc_ignores_preamble():
    row = parse_ndbc(SAMPLE_WITH_PREAMBLE)
    assert row["WSPD"] == "5.1"


def test_parse_ndbc_missing_values():
    row = parse_ndbc(SAMPLE_MISSING_WIND)
    assert row["WSPD"] == "MM"
    assert row["GST"] == "MM"
    assert row["WDIR"] == "MM"


def test_parse_ndbc_no_header_raises():
    with pytest.raises(ValueError, match="header"):
        parse_ndbc("no header here\nsome data row\n")


def test_parse_ndbc_no_data_raises():
    with pytest.raises(ValueError, match="data"):
        parse_ndbc("#YY  MM\n#yr  mo\n")


def test_parse_ndbc_empty_raises():
    with pytest.raises(ValueError):
        parse_ndbc("")


# --- obs_age_minutes ---

def test_obs_age_minutes_recent():
    now = datetime.now(timezone.utc)
    row = {
        "YY": f"{now.year - 2000:02d}",
        "MM": f"{now.month:02d}",
        "DD": f"{now.day:02d}",
        "hh": f"{now.hour:02d}",
        "mm": f"{now.minute:02d}",
    }
    age = obs_age_minutes(row)
    assert age is not None
    assert 0 <= age <= 2


def test_obs_age_minutes_one_hour_ago():
    t = datetime.now(timezone.utc) - timedelta(hours=1)
    row = {
        "YY": f"{t.year - 2000:02d}",
        "MM": f"{t.month:02d}",
        "DD": f"{t.day:02d}",
        "hh": f"{t.hour:02d}",
        "mm": f"{t.minute:02d}",
    }
    age = obs_age_minutes(row)
    assert 58 <= age <= 62


def test_obs_age_minutes_missing_fields():
    assert obs_age_minutes({}) is None


def test_obs_age_minutes_bad_values():
    assert obs_age_minutes({"YY": "MM", "MM": "05", "DD": "28", "hh": "12", "mm": "00"}) is None
