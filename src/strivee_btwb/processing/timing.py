"""Durations as Strivee writes them and as a training log reads them."""

import re

# "Warm-up" / "échauffement" and "cooldown" / "retour au calme": a part or line
# that leads into the work or winds down after it, never the work itself.
WARM_UP_RE = re.compile(r"warm[\s-]?up|[ée]chauffement", re.IGNORECASE)
COOLDOWN_RE = re.compile(r"cool[\s-]?down|retour au calme", re.IGNORECASE)


def seconds(value: str, unit: str) -> int:
    """Seconds in "<value> <unit>", the unit being any spelling of min or sec."""
    return int(value) * (1 if unit.lower().startswith("s") else 60)


def clock(duration: float) -> str:
    """Format a duration the way a training log reads it: 51:07, or 1:02:30."""
    total = round(duration)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
