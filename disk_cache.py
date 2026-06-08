"""
Persistent disk cache for weather snapshots.

Snapshots are stored as human-readable JSON.  Writes are atomic — the new
content is written to a temp file in the same directory as the target, then
``os.replace()`` swaps it in, so a crash or power loss during the write
can never corrupt the last good snapshot.

All public functions are safe to call when the cache file is missing,
unreadable, partially written, or contains garbage — they log a warning and
return a safe default rather than raising.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

_TIMESTAMP_KEY = "_saved_at"

# State fields that are worth persisting across restarts / network outages.
# History lists are included so the trend arrow and pressure chart survive
# brief connectivity gaps; alerts are excluded (they have their own timer).
CACHE_FIELDS = (
    "wind", "gust", "wdir", "age",
    "wtmp", "wvht", "atmp", "dpd", "dewp",
    "pres", "pres_history", "gust_history",
)


def save_snapshot(path: str, snapshot: dict) -> None:
    """Write *snapshot* to *path* atomically.

    Adds a ``_saved_at`` Unix-timestamp field before writing so freshness
    can be checked later with :func:`snapshot_is_fresh`.

    Does nothing (and logs a warning) if the directory cannot be created or
    the write fails for any reason.
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logging.warning("Cache: cannot create directory %s: %s", target.parent, exc)
        return

    data = {k: snapshot[k] for k in snapshot if not k.startswith("_")}
    data[_TIMESTAMP_KEY] = time.time()

    try:
        fd, tmp_path = tempfile.mkstemp(dir=target.parent, prefix=".tmp-pontoon-")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2, default=str)
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        logging.warning("Cache: failed to save snapshot to %s: %s", path, exc)
        return

    logging.debug("Cache: snapshot saved to %s", path)


def load_snapshot(path: str) -> dict | None:
    """Load a cached snapshot from *path*.

    Returns the parsed dict (including the ``_saved_at`` field) or ``None``
    if the file is missing, empty, invalid JSON, or unreadable.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, ValueError) as exc:
        logging.warning("Cache: invalid JSON in %s: %s", path, exc)
        return None
    except OSError as exc:
        logging.warning("Cache: cannot read %s: %s", path, exc)
        return None

    if not isinstance(data, dict):
        logging.warning("Cache: unexpected format in %s — ignoring", path)
        return None

    return data


def snapshot_is_fresh(snapshot: dict, max_age_seconds: int) -> bool:
    """Return ``True`` if *snapshot* was saved within *max_age_seconds* of now."""
    raw = snapshot.get(_TIMESTAMP_KEY)
    if raw is None:
        return False
    try:
        saved_at = float(raw)
    except (TypeError, ValueError):
        return False
    return (time.time() - saved_at) <= max_age_seconds
