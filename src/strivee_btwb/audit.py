"""The accessory audit: the week's per-muscle set volume and the work that fills its gaps."""

import logging
import math
import sys
from datetime import date, timedelta

from .btwb import (
    fetch_completed_titles,
    fetch_logged_loads,
    post_week,
)
from .cache import (
    load_days,
    load_formatted_day,
    load_sets_day,
    parsed_source_mtime_ns,
    save_sets_day,
    sets_fingerprint,
)
from .core import config
from .core.llm import LLMUnavailableError
from .core.models import (
    DayProgramming,
    WeeklyProgramming,
)
from .pipeline import WEEKDAYS, norm_title, short_to_date, week_start
from .processing import extract_sets
from .processing.accessory import (
    ACCESSORY_BLOCK_NAME,
    Prescription,
    build_block,
    plan_accessory,
    target_reps,
)
from .processing.loads import Load, loads_by_movement
from .processing.volume import (
    METCON_CAP,
    MuscleVolume,
    WorkSet,
    pool_movements,
    weekly_volume,
)

logger = logging.getLogger(__name__)


def week_for_audit(days: list[str], ws: date) -> tuple[WeeklyProgramming, list[str]]:
    """Load the week to audit, preferring each day's level-selected formatting.

    A day that has been previewed is counted as the athlete will actually train
    it; one that has not falls back to the parsed RX text. Returns the week plus
    the day labels that fell back, so the report can say so rather than quietly
    counting a level the athlete does not do.
    """
    parsed = {d.day_label: d for d in load_days(days, ws).days}
    ordered: list[DayProgramming] = []
    fell_back: list[str] = []
    for label in days:
        formatted = load_formatted_day(ws, label, parsed_source_mtime_ns(ws, label))
        if formatted is not None:
            ordered.append(formatted)
        elif label in parsed:
            ordered.append(parsed[label])
            fell_back.append(label)
    return WeeklyProgramming(week_start=ws, days=ordered), fell_back


def week_work_sets(
    week: WeeklyProgramming, completed: dict[str, set[str]] | None = None
) -> list[WorkSet]:
    """Extract every block's sets for the week, reusing the per-day cache.

    When *completed* is given, only sets from blocks logged as done on BTWB are
    returned. The filter is applied after caching, not before: the cache stays
    keyed on the whole day's text, so running with and without it costs no extra
    model calls.
    """
    collected: list[WorkSet] = []
    for day in week.days:
        fingerprint = sets_fingerprint(day)
        cached = load_sets_day(day, week.week_start, fingerprint)
        if cached is not None:
            logger.info("Reusing cached set extraction for %s", day.day_label)
            day_sets = cached
        else:
            logger.info("Extracting sets for %s (%d block(s))", day.day_label, len(day.blocks))
            day_sets = [s for block in day.blocks for s in extract_sets(block)]
            save_sets_day(day, week.week_start, fingerprint, day_sets)
        if completed is not None:
            done = {norm_title(t) for t in completed.get(day.date.isoformat(), set())}
            skipped = {b.name for b in day.blocks if norm_title(b.name) not in done}
            if skipped:
                logger.info(
                    "%s — not logged as done: %s", day.day_label, ", ".join(sorted(skipped))
                )
            day_sets = [s for s in day_sets if norm_title(s.source) in done]
        collected.extend(day_sets)
    return collected


def _log_gap_prescription(
    volumes: dict[str, MuscleVolume],
    location: str,
    lifted: dict[str, list[Load]] | None,
) -> None:
    """List what to do to close each muscle's gap, with last session's load."""
    gaps = [v for v in volumes.values() if v.gap > 0]
    logger.info("")
    if not gaps:
        logger.info("  Every muscle is at target — no accessory work needed this week.")
        return
    logger.info("  ── To close the gap at the %s ──", location)
    for vol in gaps:
        options = pool_movements(vol.muscle, location)
        count = math.ceil(vol.gap)
        unit = "set " if count == 1 else "sets"  # trailing space keeps the column aligned
        if not options:
            logger.warning(
                "  %-26s %2d %s   no %s option in the pool", vol.label, count, unit, location
            )
            continue
        # Two pool entries can share one BTWB name (the same cable movement set
        # up two ways), and printing it twice reads as a bug in the report.
        by_name: dict[str, str] = {}
        for movement in options:
            by_name.setdefault(movement["btwb_name"], target_reps(movement["reps"]))
        picks = " / ".join(f"{name} {reps}" for name, reps in list(by_name.items())[:2])
        logger.info("  %-26s %2d %s   %s", vol.label, count, unit, picks)
        for name in list(by_name)[:2]:
            previous = (lifted or {}).get(name.casefold())
            if previous:
                logger.info(
                    "  %-26s          last %s: %s",
                    "",
                    name,
                    ", ".join(str(load) for load in previous),
                )


def log_audit(
    ws: date,
    volumes: dict[str, MuscleVolume],
    unlisted: list[str],
    location: str,
    fell_back: list[str],
    actual: bool = False,
    plan_ws: date | None = None,
    lifted: dict[str, list[Load]] | None = None,
) -> None:
    """Print the week's per-muscle volume and what it would take to close the gap."""
    counted = "logged as done" if actual else "as programmed"
    logger.info("=" * 68)
    logger.info("  Accessory audit — week starting %s  (%s, %s)", ws, location, counted)
    if plan_ws is not None and plan_ws != ws:
        logger.info("  Measured as the baseline for the week starting %s", plan_ws)
    logger.info("=" * 68)
    logger.info("  Hard sets at 0-2 RIR. Conditioning counts 0.25/set, capped at")
    logger.info("  %.1f per muscle per week; heavy singles and skill work count 0.", METCON_CAP)
    if fell_back:
        logger.warning(
            "  %s counted at RX — not previewed, so no level was chosen.", ", ".join(fell_back)
        )
    logger.info("")
    logger.info("  %-26s %6s %6s %6s", "MUSCLE", "TARGET", "EMF", "GAP")
    for vol in volumes.values():
        logger.info("  %-26s %6.1f %6.1f %6.1f", vol.label, vol.target, vol.credited, vol.gap)
        if vol.sources:
            top = ", ".join(f"{name} {credit:.1f}" for name, credit in vol.sources[:3])
            logger.info("  %-26s        from %s", "", top)
        if vol.metcon_raw > METCON_CAP:
            logger.info(
                "  %-26s        conditioning capped: %.1f → %.1f",
                "",
                vol.metcon_raw,
                vol.metcon,
            )

    _log_gap_prescription(volumes, location, lifted)

    if unlisted:
        logger.info("")
        logger.info("  ── Not credited (absent from movement_muscles.json) ──")
        for name in unlisted:
            logger.info("      %s", name)
        logger.info("  Add any of these that train a tracked muscle, then re-run.")


def accessory_week(ws: date, plan: dict[str, list[Prescription]]) -> WeeklyProgramming:
    """Wrap the planned accessory work as a week ``post_week`` can take as-is."""
    days = [
        DayProgramming(
            date=short_to_date(label, ws),
            day_label=label,
            blocks=[build_block(entries)],
        )
        for label, entries in plan.items()
        if entries
    ]
    return WeeklyProgramming(week_start=ws, days=days)


def log_accessory_plan(week: WeeklyProgramming) -> None:
    """Show the accessory blocks exactly as BTWB will receive them."""
    logger.info("")
    logger.info("  ── Accessory blocks to post ──")
    for day in week.days:
        for block in day.blocks:
            logger.info("  %s %s — [%s]", day.day_label.upper(), day.date, block.name)
            for line in block.content.splitlines():
                logger.info("      %s", line)


def _confirm_accessory(week: WeeklyProgramming) -> bool:
    total = sum(len(d.blocks) for d in week.days)
    dates = ", ".join(f"{d.day_label} {d.date}" for d in week.days)
    answer = input(f"\nPost {total} accessory block(s) to BTWB on {dates}? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def _audit_weeks(ws: date, from_last_week: bool) -> tuple[date, date]:
    """Return (week to measure, week to plan into).

    They are normally the same week. ``--from-last-week`` separates them because
    this week's delivery is not knowable until this week is over: the most recent
    finished week is the best available estimate of what the coming one will
    leave untrained, and the gap it measures is structural enough for that to
    hold — side delts and calves get nothing every week regardless.
    """
    return (ws - timedelta(days=7), ws) if from_last_week else (ws, ws)


def _crossfit_only(week: WeeklyProgramming) -> WeeklyProgramming:
    """Drop accessory blocks from a week being measured as the baseline.

    The baseline has to be what CrossFit delivered and nothing else. Counting
    last week's accessory work into it makes the system undo itself: five sets
    one week, a satisfied target and zero the next, five again the week after —
    half the target on average, in a loop that looks correct at every step.
    """
    trimmed = []
    for day in week.days:
        keep = [b for b in day.blocks if b.name != ACCESSORY_BLOCK_NAME]
        if len(keep) != len(day.blocks):
            logger.info("%s — accessory work excluded from the baseline", day.day_label)
        trimmed.append(DayProgramming(date=day.date, day_label=day.day_label, blocks=keep))
    return WeeklyProgramming(week_start=week.week_start, days=trimmed)


def _loads_lifted(week: WeeklyProgramming) -> dict[str, list[Load]]:
    """What was lifted for each movement across the measured week's logged sessions.

    Best effort: a failure here costs a hint next to a prescription, not the
    audit, so it is reported and swallowed rather than aborting the run.
    """
    try:
        logged = fetch_logged_loads(
            config.BTWB_EMAIL,
            config.BTWB_PASSWORD,
            [d.date.isoformat() for d in week.days],
        )
    except Exception as e:
        logger.warning("Could not read logged loads: %s", e)
        return {}
    lifted: dict[str, list[Load]] = {}
    for by_title in logged.values():
        for rows, result in by_title.values():
            for movement, loads in loads_by_movement(rows, result).items():
                lifted.setdefault(norm_title(movement).casefold(), []).extend(loads)
    return lifted


def _completion_for(week: WeeklyProgramming) -> dict[str, set[str]]:
    """Read from BTWB which of the week's blocks were actually logged as done."""
    if not config.BTWB_EMAIL or not config.BTWB_PASSWORD:
        logger.error("--actual reads your BTWB calendar; set BTWB_EMAIL / BTWB_PASSWORD in .env")
        sys.exit(1)
    logger.info("Reading BTWB for what was actually logged as done…")
    try:
        return fetch_completed_titles(
            config.BTWB_EMAIL,
            config.BTWB_PASSWORD,
            [d.date.isoformat() for d in week.days],
            headless=True,
        )
    except Exception as e:
        # Falling back to the plan while the header still says "logged as done"
        # would be a silent lie about what the numbers mean.
        logger.error("Could not read completion from BTWB: %s", e)
        sys.exit(1)


def _post_accessory(planned: WeeklyProgramming, yes: bool, headless: bool, dry_run: bool) -> None:
    """Send the planned accessory blocks to BTWB, confirming first unless told not to."""
    if not dry_run and (not config.BTWB_EMAIL or not config.BTWB_PASSWORD):
        logger.error("BTWB_EMAIL and BTWB_PASSWORD must be set in .env")
        sys.exit(1)
    if not dry_run and not yes and not _confirm_accessory(planned):
        logger.info("Nothing posted.")
        return
    try:
        results = post_week(
            week=planned,
            email=config.BTWB_EMAIL,
            password=config.BTWB_PASSWORD,
            headless=headless,
            dry_run=dry_run,
            # Accessory movement names are exactly the ones BTWB's AI parser
            # resolves to the wrong exercise, so enter them through its search.
            exact_movements=True,
        )
    except Exception as e:  # AuthenticationError included — every failure aborts the same way
        logger.error("%s", e)
        sys.exit(1)
    verb = "would be posted" if dry_run else "posted"
    logger.info("Done — %d accessory block(s) %s", len(results), verb)


def do_audit(
    days: list[str],
    ws: date | None = None,
    location: str = "gym",
    on: list[str] | None = None,
    post: bool = False,
    yes: bool = False,
    headless: bool = False,
    dry_run: bool = False,
    actual: bool = False,
    from_last_week: bool = False,
) -> None:
    ws = ws or week_start()
    measure_ws, plan_ws = _audit_weeks(ws, from_last_week)
    # Measuring a past week only makes sense against what was actually done there.
    actual = actual or from_last_week
    if post and not on:
        logger.error("--post needs --on to say which day(s) the accessory work goes on")
        sys.exit(1)
    if on:
        # A typo like "Tues" would otherwise plan a day that silently never posts.
        unknown = [label for label in on if label not in WEEKDAYS]
        if unknown:
            logger.error("Unknown day(s) in --on: %s (expected %s)", unknown, ", ".join(WEEKDAYS))
            sys.exit(1)

    week, fell_back = week_for_audit(days, measure_ws)
    if not week.days:
        logger.error(
            "No cached analysis for the week of %s — run: strivee-btwb analyse --week %s",
            measure_ws,
            measure_ws,
        )
        sys.exit(1)
    week = _crossfit_only(week)
    completed = _completion_for(week) if actual else None

    try:
        work_sets = week_work_sets(week, completed)
    except LLMUnavailableError as e:
        logger.error("%s", e)
        sys.exit(1)
    volumes, unlisted = weekly_volume(work_sets)
    # Only when BTWB is being read anyway: what was lifted last time is the whole
    # progression signal when every set goes to failure against a fixed rep target.
    lifted = _loads_lifted(week) if actual else None
    log_audit(measure_ws, volumes, unlisted, location, fell_back, actual, plan_ws, lifted)

    if not on:
        return

    planned = accessory_week(plan_ws, plan_accessory(volumes, location, on))
    if not planned.days:
        logger.info("")
        logger.info("  Nothing to add — every muscle is already at target.")
        return
    log_accessory_plan(planned)

    if not post:
        logger.info("")
        logger.info("  Re-run with --post to send these to BTWB.")
        return

    _post_accessory(planned, yes, headless, dry_run)
