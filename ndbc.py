from __future__ import annotations

from datetime import datetime, timezone

MS_TO_MPH = 2.23694
M_TO_FT   = 3.28084
_COMPASS: list[str] = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def ms_to_mph(ms: float) -> float:
    """Convert metres per second to miles per hour."""
    return ms * MS_TO_MPH


def celsius_to_f(c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return c * 9 / 5 + 32


def m_to_ft(m: float) -> float:
    """Convert metres to feet."""
    return m * M_TO_FT


def wind_direction(deg_str: str) -> str:
    """Convert NDBC WDIR string (degrees, 'MM', or '999') to compass label."""
    if deg_str in ("MM", "999"):
        return "---"
    return _COMPASS[round(int(deg_str) / 45) % 8]


def parse_ndbc(txt: str) -> dict[str, str]:
    """Return the most recent observation as a column-name → value dict.

    Finds the header by the '#YY' sentinel so it is robust against NDBC
    inserting preamble comment lines (e.g. storm advisories).
    """
    lines = txt.splitlines()
    header_line = next((l for l in lines if l.startswith("#YY")), None)
    if header_line is None:
        raise ValueError("NDBC header row not found")
    cols = header_line.lstrip("#").split()
    data_line = next((l for l in lines if l.strip() and not l.startswith("#")), None)
    if data_line is None:
        raise ValueError("No NDBC data rows found")
    return dict(zip(cols, data_line.split()))


def _year(yy: str) -> int:
    """NDBC realtime2 files use a 4-digit year under the ``#YY`` header; some
    older/historical files use 2 digits. Accept both."""
    year = int(yy)
    return year + 2000 if year < 100 else year


def obs_time(row: dict[str, str]) -> str | None:
    """Return the observation's UTC timestamp as ``'YYYY-MM-DD hh:mm'``, or None."""
    try:
        return (f"{_year(row['YY']):04d}-{int(row['MM']):02d}-{int(row['DD']):02d} "
                f"{int(row['hh']):02d}:{int(row['mm']):02d}")
    except (KeyError, ValueError):
        return None


def obs_age_minutes(row: dict[str, str]) -> int | None:
    """Return how many minutes ago the observation was recorded (UTC), or None."""
    try:
        obs = datetime(
            _year(row["YY"]),
            int(row["MM"]),
            int(row["DD"]),
            int(row["hh"]),
            int(row["mm"]),
            tzinfo=timezone.utc,
        )
        return max(0, int((datetime.now(timezone.utc) - obs).total_seconds() / 60))
    except (KeyError, ValueError):
        return None
