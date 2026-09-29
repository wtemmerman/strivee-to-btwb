"""The on-disk caches between steps: captures, parsed days, formatted days, set extractions.

Each is versioned, so a cache written by an older parser or formatter is
recomputed rather than silently reused.
"""

import hashlib
import json
import logging
from datetime import date
from pathlib import Path

from .core import config
from .core.models import (
    RX,
    ClassicSets,
    DayProgramming,
    ErgIntervals,
    ProgrammingBlock,
    WeeklyProgramming,
    plan_from_json,
    plan_to_json,
)
from .processing.volume import (
    WorkSet,
)

logger = logging.getLogger(__name__)

# Bump when the parsed-cache JSON shape or the parser semantics change, so that
# preview/post warn instead of silently consuming output from an older parser.
# 2: blocks carry the INTER+/INTER prescriptions in their own fields instead of
# having them folded into instruction.
CACHE_SCHEMA_VERSION = 2

# Bump when the formatting (clean_week / format_for_btwb / format prompt) changes,
# so a stale formatted cache is recomputed instead of silently reused.
# 2: blocks record the difficulty level their content was selected from.
# 3: "+"-joined blocks are split into one workout per part.
# 4: erg interval blocks carry the plan the classic builder posts.
# 5: every note opens with the prescription as Strivee wrote it.
# 6: single-movement set schemes carry the plan the classic builder posts.
# 7: set plans carry their load, rep-max and EMOM shape.
FORMATTED_SCHEMA_VERSION = 7

# Bump when the set-extraction prompt or WorkSet shape changes, so a stale
# per-day set cache is re-extracted instead of silently reused by the audit.
# 5: classic-builder blocks give their sets from the plan, not the model.
SETS_SCHEMA_VERSION = 5


# ── Cache ─────────────────────────────────────────────────────────────────────


def save_day(day: DayProgramming, ws: date) -> Path:
    """Persist a parsed day as JSON inside the per-week parsed sub-directory."""
    out = config.PARSED_DIR / ws.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"parsed_{day.date.isoformat()}_{day.day_label}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": CACHE_SCHEMA_VERSION,
                "date": day.date.isoformat(),
                "day_label": day.day_label,
                "blocks": [
                    {
                        "name": b.name,
                        "content": b.content,
                        "instruction": b.instruction,
                        "inter_plus": b.inter_plus,
                        "inter": b.inter,
                    }
                    for b in day.blocks
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return path


def load_days(days: list[str], ws: date) -> WeeklyProgramming:
    """Load cached per-day JSON files from the per-week parsed directory."""
    folder = config.PARSED_DIR / ws.isoformat()
    parsed = []
    for label in days:
        matches = sorted(folder.glob(f"parsed_*_{label}.json"))
        if not matches:
            logger.warning("No cached analysis for %s", label)
            continue
        data = json.loads(matches[-1].read_text())
        version = data.get("schema_version")
        if version != CACHE_SCHEMA_VERSION:
            logger.warning(
                "Cache %s has schema_version %r (expected %d) — it may be stale; "
                "re-run 'strivee-btwb analyse' if results look wrong.",
                matches[-1].name,
                version,
                CACHE_SCHEMA_VERSION,
            )
        try:
            day = DayProgramming(
                date=date.fromisoformat(data["date"]),
                day_label=data["day_label"],
                blocks=[
                    ProgrammingBlock(
                        name=b["name"],
                        content=b["content"],
                        instruction=b.get("instruction", ""),
                        inter_plus=b.get("inter_plus", ""),
                        inter=b.get("inter", ""),
                    )
                    for b in data["blocks"]
                ],
            )
        except (KeyError, ValueError) as e:
            # Malformed / older-shape / empty-name cache: skip this day with a clear
            # message rather than crashing the whole preview/post with a traceback.
            logger.warning(
                "Skipping unreadable cache %s (%s) — re-run: strivee-btwb analyse",
                matches[-1].name,
                e,
            )
            continue
        logger.info("Loaded cache: %s", matches[-1].name)
        parsed.append(day)
    return WeeklyProgramming(week_start=ws, days=parsed)


def parsed_source_mtime_ns(ws: date, day_label: str) -> int | None:
    """Modification time (ns) of the parsed file load_days would read for a day.

    Used as the freshness key for the formatted cache: if the parsed source is
    re-analysed (newer mtime), the formatted cache for that day is recomputed.
    """
    folder = config.PARSED_DIR / ws.isoformat()
    matches = sorted(folder.glob(f"parsed_*_{day_label}.json"))
    return matches[-1].stat().st_mtime_ns if matches else None


def save_formatted_day(day: DayProgramming, ws: date, source_mtime_ns: int | None) -> Path:
    """Persist a cleaned+formatted day so post can reuse preview's LLM output."""
    out = config.FORMATTED_DIR / ws.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"formatted_{day.date.isoformat()}_{day.day_label}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": FORMATTED_SCHEMA_VERSION,
                "source_mtime_ns": source_mtime_ns,
                "date": day.date.isoformat(),
                "day_label": day.day_label,
                "blocks": [
                    {
                        "name": b.name,
                        "content": b.content,
                        "instruction": b.instruction,
                        "level": b.level,
                        "erg": plan_to_json(b.erg),
                        "sets": plan_to_json(b.sets),
                    }
                    for b in day.blocks
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return path


def load_formatted_day(
    ws: date, day_label: str, expected_mtime_ns: int | None
) -> DayProgramming | None:
    """Return the cached formatted day iff it is fresh, else None.

    Fresh means: the file exists, its schema matches the current formatter, and
    its recorded parsed-source mtime equals the parsed file's current mtime (so
    re-analysing a day invalidates its formatted cache).
    """
    folder = config.FORMATTED_DIR / ws.isoformat()
    matches = sorted(folder.glob(f"formatted_*_{day_label}.json"))
    if not matches:
        return None
    try:
        data = json.loads(matches[-1].read_text())
    except (OSError, ValueError):
        return None
    if data.get("schema_version") != FORMATTED_SCHEMA_VERSION:
        return None
    if data.get("source_mtime_ns") != expected_mtime_ns:
        logger.info("Formatted cache for %s is stale (re-analysed) — reformatting", day_label)
        return None
    return DayProgramming(
        date=date.fromisoformat(data["date"]),
        day_label=data["day_label"],
        blocks=[
            ProgrammingBlock(
                name=b["name"],
                content=b["content"],
                instruction=b.get("instruction", ""),
                level=b.get("level", RX),
                erg=plan_from_json(ErgIntervals, b.get("erg")),
                sets=plan_from_json(ClassicSets, b.get("sets")),
            )
            for b in data["blocks"]
        ],
    )


def save_text_capture(text: str, label: str, ws: date) -> Path:
    """Save a UI-dump text capture and return its path."""
    from datetime import datetime

    out = config.CAPTURES_DIR / ws.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out / f"strivee_{ts}_{label}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def load_text_captures(days: list[str], ws: date) -> dict[str, str]:
    """Load UI-dump text captures from the per-week captures directory."""
    folder = config.CAPTURES_DIR / ws.isoformat()
    result: dict[str, str] = {}
    for day in days:
        matches = sorted(folder.glob(f"strivee_*_{day}.txt"))
        if not matches:
            continue
        logger.info("Loaded text capture: %s", matches[-1].name)
        result[day] = matches[-1].read_text(encoding="utf-8")
    return result


def sets_fingerprint(day: DayProgramming) -> str:
    """Identify the exact block text a day's set extraction was made from.

    The audit reads whichever of the two caches is available, and the formatted
    one changes with the level choice as well as with re-analysis. Hashing the
    text that was actually read covers both without the sets cache having to know
    which cache it came from.
    """
    payload = "\x00".join(f"{b.name}\x01{b.content}" for b in day.blocks)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def save_sets_day(day: DayProgramming, ws: date, fingerprint: str, sets: list[WorkSet]) -> Path:
    """Cache a day's extracted sets so re-running the audit costs no LLM calls."""
    out = config.PARSED_DIR / ws.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"sets_{day.date.isoformat()}_{day.day_label}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": SETS_SCHEMA_VERSION,
                "fingerprint": fingerprint,
                "date": day.date.isoformat(),
                "day_label": day.day_label,
                "sets": [
                    {
                        "movement": s.movement,
                        "sets": s.sets,
                        "reps": s.reps,
                        "block_type": s.block_type,
                        "source": s.source,
                    }
                    for s in sets
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return path


def load_sets_day(day: DayProgramming, ws: date, fingerprint: str) -> list[WorkSet] | None:
    """Return a day's cached sets iff they were extracted from this exact text."""
    path = config.PARSED_DIR / ws.isoformat() / f"sets_{day.date.isoformat()}_{day.day_label}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if data.get("schema_version") != SETS_SCHEMA_VERSION:
        return None
    if data.get("fingerprint") != fingerprint:
        logger.info("Set cache for %s is stale — re-extracting", day.day_label)
        return None
    return [
        WorkSet(
            movement=s["movement"],
            sets=s["sets"],
            reps=s.get("reps", ""),
            block_type=s["block_type"],
            source=s.get("source", ""),
        )
        for s in data["sets"]
    ]
