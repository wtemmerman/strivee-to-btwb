"""Cache raw Garmin activities per week, so re-running a sync costs no API calls.

The raw payload is stored untouched rather than the mapped sessions: Garmin is
rate-limited and the mapping is cheap, so every mapping change should be
re-runnable against last week's real data without asking Garmin again.
"""

import json
import logging
from datetime import date
from pathlib import Path

from ..core import config

logger = logging.getLogger("garmin")

SCHEMA_VERSION = 1
"""Bump when the cached shape changes, so a stale file is re-fetched rather than
silently mapped by code that expects different fields."""


def _path(week_start: date) -> Path:
    return config.GARMIN_DIR / week_start.isoformat() / "activities.json"


def save_week(week_start: date, activities: list[dict]) -> Path:
    """Persist one week's raw activities (laps included where they were fetched)."""
    path = _path(week_start)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "week_start": week_start.isoformat(),
                "activities": activities,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    return path


def load_week(week_start: date) -> list[dict] | None:
    """Return a week's cached activities, or None when there is no usable cache."""
    path = _path(week_start)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        logger.info("Garmin cache %s is from an older schema — re-fetching", path.name)
        return None
    return data["activities"]
