"""
Orchestration pipeline: cache I/O, week processing, and step implementations.

Each step (capture, analyse, preview, post) is a standalone function that reads
from the previous step's cache, so steps can be run independently or restarted.
"""

import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from PIL import Image

import ollama

from .btwb import AuthenticationError, post_week
from .capture import (
    capture_day_screenshots,
    launch_scrcpy,
    launch_strivee,
    navigate_to_week,
    save_capture,
    scroll_to_top,
    stitch_vertical,
)
from .core import config
from .core.models import DayProgramming, ProgrammingBlock, WeeklyProgramming
from .processing import format_for_btwb
from .vision import extract_day_programming

logger = logging.getLogger(__name__)

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ── Date helpers ──────────────────────────────────────────────────────────────


def week_start(anchor: date | None = None) -> date:
    """Return the Monday of the week containing *anchor* (defaults to today)."""
    d = anchor or date.today()
    return d - timedelta(days=d.weekday())


def short_to_date(day_short: str, ws: date | None = None) -> date:
    return (ws or week_start()) + timedelta(days=WEEKDAYS.index(day_short))


def parse_days(raw: str | None) -> list[str]:
    return [d.strip() for d in raw.split(",")] if raw else WEEKDAYS[:6]


# ── Cache ─────────────────────────────────────────────────────────────────────


def save_day(day: DayProgramming, ws: date) -> Path:
    """Persist a parsed day as JSON inside the per-week parsed sub-directory."""
    out = config.PARSED_DIR / ws.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"parsed_{day.date.isoformat()}_{day.day_label}.json"
    path.write_text(
        json.dumps(
            {
                "date": day.date.isoformat(),
                "day_label": day.day_label,
                "blocks": [{"name": b.name, "content": b.content} for b in day.blocks],
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
        logger.info("Loaded cache: %s", matches[-1].name)
        parsed.append(
            DayProgramming(
                date=date.fromisoformat(data["date"]),
                day_label=data["day_label"],
                blocks=[
                    ProgrammingBlock(name=b["name"], content=b["content"])
                    for b in data["blocks"]
                ],
            )
        )
    return WeeklyProgramming(week_start=ws, days=parsed)


def load_captures(days: list[str], ws: date) -> dict[str, list[Image.Image]]:
    """Load stitched PNG captures from the per-week captures directory."""
    folder = config.CAPTURES_DIR / ws.isoformat()
    result: dict[str, list[Image.Image]] = {}
    for day in days:
        matches = sorted(folder.glob(f"strivee_*_{day}.png"))
        if not matches:
            logger.warning("No saved capture for %s", day)
            continue
        logger.info("Loaded capture: %s", matches[-1].name)
        result[day] = [Image.open(matches[-1])]
    return result


# ── Week processing ───────────────────────────────────────────────────────────


def llm_format_week(week: WeeklyProgramming) -> WeeklyProgramming:
    """Apply LLM-based Rx extraction and BTWB formatting to every block."""
    days = []
    try:
        for day in week.days:
            logger.info("Formatting %s with LLM…", day.day_label)
            blocks = [format_for_btwb(b) for b in day.blocks]
            blocks = [b for b in blocks if b.content.strip()]
            if blocks:
                days.append(DayProgramming(date=day.date, day_label=day.day_label, blocks=blocks))
    except KeyboardInterrupt:
        logger.info("Interrupted — unloading format model from Ollama…")
        try:
            ollama.generate(model=config.OLLAMA_FORMAT_MODEL, keep_alive=0)
        except Exception:
            pass
        raise
    return WeeklyProgramming(week_start=week.week_start, days=days)


def clean_week(week: WeeklyProgramming) -> WeeklyProgramming:
    """Remove empty blocks and merge consecutive blocks with the same name."""
    cleaned_days = []
    for day in week.days:
        merged: list[ProgrammingBlock] = []
        for block in day.blocks:
            if not block.content.strip():
                continue
            if merged and merged[-1].name.lower() == block.name.lower():
                merged[-1] = ProgrammingBlock(
                    name=merged[-1].name,
                    content=merged[-1].content + "\n" + block.content,
                )
            else:
                merged.append(ProgrammingBlock(name=block.name, content=block.content))
        if merged:
            cleaned_days.append(
                DayProgramming(date=day.date, day_label=day.day_label, blocks=merged)
            )
    return WeeklyProgramming(week_start=week.week_start, days=cleaned_days)


# ── Display ───────────────────────────────────────────────────────────────────


def log_summary(week: WeeklyProgramming) -> None:
    logger.info("=" * 60)
    logger.info("  Week starting %s  (%d days)", week.week_start, len(week.days))
    logger.info("=" * 60)
    for day in week.days:
        logger.info("  %s — %s", day.day_label.upper(), day.date)
        for block in day.blocks:
            first_line = block.content.splitlines()[0] if block.content else ""
            logger.info("    [%s] %s", block.name, first_line)


def log_preview(week: WeeklyProgramming) -> None:
    logger.info("=" * 60)
    logger.info("  BTWB Preview — Week starting %s", week.week_start)
    logger.info("=" * 60)
    for day in week.days:
        logger.info("  %s — %s  (%d block(s))", day.day_label.upper(), day.date, len(day.blocks))
        for block in day.blocks:
            logger.info("  [%s]", block.name)
            for line in block.content.splitlines():
                logger.info("      %s", line)


# ── Steps ─────────────────────────────────────────────────────────────────────


def do_capture(days: list[str], no_scrcpy: bool, ws: date | None = None) -> None:
    serial = config.ANDROID_SERIAL
    ws = ws or week_start()
    scrcpy_proc = None

    if not no_scrcpy:
        logger.info("Launching scrcpy...")
        try:
            scrcpy_proc = launch_scrcpy(serial)
        except FileNotFoundError:
            logger.warning("scrcpy not found — install with: brew install scrcpy")

    logger.info("Launching Strivee on device...")
    try:
        launch_strivee(serial)
    except Exception as e:
        logger.error("%s", e)
        if scrcpy_proc:
            scrcpy_proc.terminate()
        sys.exit(1)

    scroll_to_top(serial)  # also waits for the app to fully render after launch
    navigate_to_week(ws, serial)

    debug_dir = None
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        debug_dir = config.CAPTURES_DIR / ws.isoformat() / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("Raw frames will be saved to %s", debug_dir)

    logger.info("Capturing %d day(s): %s", len(days), ", ".join(days))
    saved = 0
    for day in days:
        try:
            scroll_to_top(serial)
            frames = capture_day_screenshots(day, serial, config.MAX_SCROLLS, debug_dir=debug_dir)
            path = save_capture(stitch_vertical(frames), label=day, week_start=ws)
            logger.info("%s saved -> %s (%d frame(s))", day, path.name, len(frames))
            saved += 1
        except Exception as e:
            logger.error("%s: capture failed — %s", day, e)

    if scrcpy_proc:
        scrcpy_proc.terminate()

    if saved == 0:
        logger.error("No days captured successfully")
        sys.exit(1)

    logger.info("Capture done (%d/%d days)", saved, len(days))


def do_analyse(days: list[str], ws: date | None = None) -> None:
    ws = ws or week_start()
    day_images = load_captures(days, ws)
    if not day_images:
        logger.error(
            "No captures found in %s/%s/ — run: strivee-btwb capture",
            config.CAPTURES_DIR,
            ws.isoformat(),
        )
        sys.exit(1)

    logger.info("Starting vision analysis with model '%s'", config.OLLAMA_MODEL)
    for day_short, frames in day_images.items():
        try:
            day_prog = extract_day_programming(
                images=frames,
                day_label=day_short,
                target_date=short_to_date(day_short, ws),
            )
            if not day_prog.blocks and config.OLLAMA_FALLBACK_MODEL:
                logger.warning(
                    "%s: no blocks from primary model — retrying with fallback '%s'",
                    day_short,
                    config.OLLAMA_FALLBACK_MODEL,
                )
                day_prog = extract_day_programming(
                    images=frames,
                    day_label=day_short,
                    target_date=short_to_date(day_short, ws),
                    model=config.OLLAMA_FALLBACK_MODEL,
                )
            if day_prog.blocks:
                path = save_day(day_prog, ws)
                logger.info("%s cached -> %s", day_short, path.name)
            else:
                logger.warning("%s: no blocks found after fallback — skipping", day_short)
        except Exception as e:
            logger.error("%s: analysis failed — %s", day_short, e)

    logger.info("Analysis done")


def do_preview(days: list[str], ws: date | None = None) -> None:
    week = load_days(days, ws or week_start())
    if not week.days:
        logger.error("No cached analysis found — run: strivee-btwb analyse")
        sys.exit(1)
    week = clean_week(week)
    week = llm_format_week(week)
    log_summary(week)
    log_preview(week)


def do_post(days: list[str], yes: bool, headless: bool, ws: date | None = None) -> None:
    week = load_days(days, ws or week_start())
    if not week.days:
        logger.error("No cached analysis found — run: strivee-btwb analyse")
        sys.exit(1)
    week = clean_week(week)
    week = llm_format_week(week)
    log_summary(week)

    if not config.BTWB_EMAIL or not config.BTWB_PASSWORD:
        logger.error("BTWB_EMAIL and BTWB_PASSWORD must be set in .env")
        sys.exit(1)

    if yes:
        approved = week.days
    else:
        answer = input("Post all days to BTWB? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            approved = week.days
        else:
            approved = []
            for day in week.days:
                ans = input(f"  Post {day.day_label} {day.date}? [y/N] ").strip().lower()
                if ans in ("y", "yes"):
                    approved.append(day)

    if not approved:
        logger.info("No days approved. Exiting.")
        sys.exit(0)

    try:
        results = post_week(
            week=week,
            email=config.BTWB_EMAIL,
            password=config.BTWB_PASSWORD,
            days=approved,
            headless=headless,
        )
    except AuthenticationError as e:
        logger.error("%s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("%s", e)
        sys.exit(1)

    logger.info("Done — %d block(s) posted successfully", len(results))
