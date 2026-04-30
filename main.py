"""
strivee-btwb: Transfer CrossFit weekly programming from Strivee to BTWB.

Commands:
    python main.py capture            # Step 1: capture phone screen via ADB
    python main.py analyse            # Step 2: run vision analysis, cache per day
    python main.py preview            # Step 3: show full content as it would go to BTWB
    python main.py post               # Step 4: post cached results to BTWB
    python main.py run                # All steps sequentially

Each step caches its output and reads from the previous step's cache, so they
can be called independently or restarted without repeating earlier work.
"""

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from PIL import Image

from src.strivee_btwb import config
from src.strivee_btwb.btwb_client import AuthenticationError, post_week
from src.strivee_btwb.capture import (
    capture_day_screenshots,
    launch_scrcpy,
    launch_strivee,
    save_capture,
    scroll_to_top,
    stitch_vertical,
)
from src.strivee_btwb.models import DayProgramming, ProgrammingBlock, WeeklyProgramming
from src.strivee_btwb.vision import extract_day_programming
from src.strivee_btwb.wod import prepare_block

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

logger = logging.getLogger("main")


# ── Logging ───────────────────────────────────────────────────────────────────


def _setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logging.root.setLevel(level)
    logging.root.addHandler(handler)
    for noisy in ("urllib3", "httpx", "playwright", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ── Date helpers ──────────────────────────────────────────────────────────────


def _week_start() -> date:
    today = date.today()
    return today - timedelta(days=today.weekday())


def _short_to_date(day_short: str) -> date:
    return _week_start() + timedelta(days=_WEEKDAYS.index(day_short))


def _parse_days(raw: str | None) -> list[str]:
    return [d.strip() for d in raw.split(",")] if raw else _WEEKDAYS[:6]


# ── Cache ─────────────────────────────────────────────────────────────────────


def _save_day(day: DayProgramming, week_start: date) -> Path:
    """Persist a parsed day as JSON inside the per-week captures sub-directory."""
    out = config.CAPTURES_DIR / week_start.isoformat()
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


def _load_days(days: list[str], week_start: date) -> WeeklyProgramming:
    """Load cached per-day JSON files from the per-week captures directory."""
    folder = config.CAPTURES_DIR / week_start.isoformat()
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
    return WeeklyProgramming(week_start=week_start, days=parsed)


def _load_captures(days: list[str], week_start: date) -> dict[str, list[Image.Image]]:
    """Load stitched PNG captures from the per-week captures directory."""
    folder = config.CAPTURES_DIR / week_start.isoformat()
    result: dict[str, list[Image.Image]] = {}
    for day in days:
        matches = sorted(folder.glob(f"strivee_*_{day}.png"))
        if not matches:
            logger.warning("No saved capture for %s", day)
            continue
        logger.info("Loaded capture: %s", matches[-1].name)
        result[day] = [Image.open(matches[-1])]
    return result


# ── Display ───────────────────────────────────────────────────────────────────


def _prepare_week(week: WeeklyProgramming) -> WeeklyProgramming:
    """Apply Rx extraction and coaching-note stripping to every block."""
    days = []
    for day in week.days:
        blocks = [prepare_block(b) for b in day.blocks]
        blocks = [b for b in blocks if b.content.strip()]
        if blocks:
            days.append(DayProgramming(date=day.date, day_label=day.day_label, blocks=blocks))
    return WeeklyProgramming(week_start=week.week_start, days=days)


def _clean_week(week: WeeklyProgramming) -> WeeklyProgramming:
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


def _print_summary(week: WeeklyProgramming) -> None:
    print(f"\n{'=' * 60}")
    print(f"  Week starting {week.week_start}  ({len(week.days)} days)")
    print(f"{'=' * 60}")
    for day in week.days:
        print(f"\n  {day.day_label.upper()} — {day.date}")
        for block in day.blocks:
            first_line = block.content.splitlines()[0] if block.content else ""
            print(f"    [{block.name}] {first_line}")
    print()


def _print_preview(week: WeeklyProgramming) -> None:
    print(f"\n{'=' * 60}")
    print(f"  BTWB Preview — Week starting {week.week_start}")
    print(f"{'=' * 60}")
    for day in week.days:
        print(f"\n  {'─' * 56}")
        print(f"  {day.day_label.upper()} — {day.date}  ({len(day.blocks)} block(s))")
        print(f"  {'─' * 56}")
        for block in day.blocks:
            print(f"\n  [{block.name}]")
            for line in block.content.splitlines():
                print(f"      {line}")
    print()


# ── Step implementations ──────────────────────────────────────────────────────


def _do_capture(days: list[str], no_scrcpy: bool) -> None:
    serial = config.ANDROID_SERIAL
    ws = _week_start()
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

    logger.info("Capturing %d day(s): %s", len(days), ", ".join(days))
    saved = 0
    for day in days:
        try:
            scroll_to_top(serial)
            frames = capture_day_screenshots(day, serial, config.MAX_SCROLLS)
            path = save_capture(stitch_vertical(frames), label=day, week_start=ws)
            logger.info("%s saved → %s (%d frame(s))", day, path.name, len(frames))
            saved += 1
        except Exception as e:
            logger.error("%s: capture failed — %s", day, e)

    if scrcpy_proc:
        scrcpy_proc.terminate()

    if saved == 0:
        logger.error("No days captured successfully")
        sys.exit(1)

    logger.info(
        "Capture done (%d/%d days) — run: python main.py analyse", saved, len(days)
    )


def _do_analyse(days: list[str]) -> None:
    ws = _week_start()
    day_images = _load_captures(days, ws)
    if not day_images:
        logger.error(
            "No captures found in %s/%s/ — run: python main.py capture",
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
                target_date=_short_to_date(day_short),
            )
            if day_prog.blocks:
                path = _save_day(day_prog, ws)
                logger.info("%s cached → %s", day_short, path.name)
            else:
                logger.warning("%s: no blocks found (rest day?)", day_short)
        except Exception as e:
            logger.error("%s: analysis failed — %s", day_short, e)

    logger.info("Analysis done — run: python main.py preview")


def _do_preview(days: list[str]) -> None:
    week = _load_days(days, _week_start())
    if not week.days:
        logger.error("No cached analysis found — run: python main.py analyse")
        sys.exit(1)
    week = _clean_week(week)
    week = _prepare_week(week)
    _print_summary(week)
    _print_preview(week)


def _do_post(days: list[str], yes: bool, headless: bool) -> None:
    week = _load_days(days, _week_start())
    if not week.days:
        logger.error("No cached analysis found — run: python main.py analyse")
        sys.exit(1)
    week = _clean_week(week)
    week = _prepare_week(week)
    _print_summary(week)

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
                ans = (
                    input(f"  Post {day.day_label} {day.date}? [y/N] ").strip().lower()
                )
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


# ── Subcommands ───────────────────────────────────────────────────────────────


def cmd_capture(args) -> None:
    _do_capture(_parse_days(args.days), args.no_scrcpy)


def cmd_analyse(args) -> None:
    _do_analyse(_parse_days(args.days))


def cmd_preview(args) -> None:
    _do_preview(_parse_days(args.days))


def cmd_post(args) -> None:
    _do_post(_parse_days(args.days), args.yes, args.headless)


def cmd_run(args) -> None:
    days = _parse_days(args.days)
    _do_capture(days, args.no_scrcpy)
    _do_analyse(days)
    _do_preview(days)
    _do_post(days, args.yes, args.headless)


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transfer Strivee CrossFit programming to BTWB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable DEBUG-level logging"
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    _days_help = "Comma-separated days to process (default: Mon–Sat)"
    _scrcpy_help = "Skip launching scrcpy (use if already open)"

    p = sub.add_parser("capture", help="Step 1 — capture phone screen via ADB")
    p.add_argument("--days", metavar="Mon,Tue,...", help=_days_help)
    p.add_argument("--no-scrcpy", action="store_true", help=_scrcpy_help)

    p = sub.add_parser("analyse", help="Step 2 — run vision analysis on saved captures")
    p.add_argument("--days", metavar="Mon,Tue,...", help=_days_help)

    p = sub.add_parser(
        "preview", help="Step 3 — show full block content as it would go to BTWB"
    )
    p.add_argument("--days", metavar="Mon,Tue,...", help=_days_help)

    p = sub.add_parser("post", help="Step 4 — post cached results to BTWB")
    p.add_argument("--days", metavar="Mon,Tue,...", help=_days_help)
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    p.add_argument(
        "--headless", action="store_true", help="Run browser without a visible window"
    )

    p = sub.add_parser(
        "run", help="Run all steps sequentially: capture → analyse → preview → post"
    )
    p.add_argument("--days", metavar="Mon,Tue,...", help=_days_help)
    p.add_argument(
        "--yes", "-y", action="store_true", help="Skip confirmation before posting"
    )
    p.add_argument(
        "--headless", action="store_true", help="Run browser without a visible window"
    )
    p.add_argument("--no-scrcpy", action="store_true", help=_scrcpy_help)

    args = parser.parse_args()
    _setup_logging(debug=args.debug)

    {
        "capture": cmd_capture,
        "analyse": cmd_analyse,
        "preview": cmd_preview,
        "post": cmd_post,
        "run": cmd_run,
    }[args.command](args)


if __name__ == "__main__":
    main()
