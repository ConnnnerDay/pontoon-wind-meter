from __future__ import annotations

from datetime import datetime, timezone

MS_TO_MPH = 2.23694
_COMPASS: list[str] = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def ms_to_mph(ms: float) -> float:
    """Convert metres per second to miles per hour."""
    return ms * MS_TO_MPH


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


def obs_age_minutes(row: dict[str, str]) -> int | None:
    """Return how many minutes ago the observation was recorded (UTC), or None."""
    try:
        obs = datetime(
            2000 + int(row["YY"]),
            int(row["MM"]),
            int(row["DD"]),
            int(row["hh"]),
            int(row["mm"]),
            tzinfo=timezone.utc,
        )
        return max(0, int((datetime.now(timezone.utc) - obs).total_seconds() / 60))
    except (KeyError, ValueError):
        return None
