"""
Orchestration pipeline: cache I/O, week processing, and step implementations.

Each step (capture, analyse, preview, post) is a standalone function that reads
from the previous step's cache, so steps can be run independently or restarted.
"""

import hashlib
import json
import logging
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import ollama

from .btwb import (
    AuthenticationError,
    delete_week,
    fetch_completed_titles,
    fetch_planned_workouts,
    post_week,
)
from .capture import (
    capture_day_as_text,
    launch_scrcpy,
    launch_strivee,
    navigate_to_week,
    reset_device_size_cache,
    scroll_to_top,
)
from .core import config
from .core.llm import LLMUnavailableError
from .core.models import (
    INTER,
    INTER_PLUS,
    LEVEL_LABELS,
    RX,
    DayProgramming,
    ProgrammingBlock,
    WeeklyProgramming,
)
from .processing import extract_sets, format_for_btwb
from .processing.accessory import (
    ACCESSORY_BLOCK_NAME,
    Prescription,
    build_block,
    plan_accessory,
    target_reps,
)
from .processing.movement_check import check_stored
from .processing.volume import (
    METCON_CAP,
    MuscleVolume,
    WorkSet,
    pool_movements,
    weekly_volume,
)
from .vision import count_block_titles, extract_day_programming_from_text

logger = logging.getLogger(__name__)

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Bump when the parsed-cache JSON shape or the parser semantics change, so that
# preview/post warn instead of silently consuming output from an older parser.
# 2: blocks carry the INTER+/INTER prescriptions in their own fields instead of
# having them folded into instruction.
CACHE_SCHEMA_VERSION = 2

# Bump when the formatting (clean_week / format_for_btwb / format prompt) changes,
# so a stale formatted cache is recomputed instead of silently reused.
# 2: blocks record the difficulty level their content was selected from.
FORMATTED_SCHEMA_VERSION = 2

# Bump when the set-extraction prompt or WorkSet shape changes, so a stale
# per-day set cache is re-extracted instead of silently reused by the audit.
SETS_SCHEMA_VERSION = 4


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


def _parsed_source_mtime_ns(ws: date, day_label: str) -> int | None:
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


# ── Week processing ───────────────────────────────────────────────────────────


def llm_format_week(week: WeeklyProgramming) -> WeeklyProgramming:
    """Apply LLM-based Rx extraction and BTWB formatting to every block.

    Kept sequential on purpose: on a single GPU these calls are prefill-bound, so
    running them concurrently contends for the GPU and is slower, not faster
    (measured ~5x slower for 4-way analyse concurrency). The real cost is removed
    by caching the formatted week (see prepare_week_for_btwb) so it runs once.
    """
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


def _merge_level(first: ProgrammingBlock, second: ProgrammingBlock, level: str) -> str:
    """Concatenate two merged blocks' prescriptions for *level*.

    A half that does not publish this level contributes its RX content instead, so
    the merged variant stays a complete workout rather than only the part that
    happened to be scaled. Empty when neither half publishes it.
    """
    if not first.level_text(level).strip() and not second.level_text(level).strip():
        return ""
    return "\n".join(
        b.level_text(level).strip() or b.content.strip() for b in (first, second)
    ).strip()


def clean_week(week: WeeklyProgramming) -> WeeklyProgramming:
    """Remove empty blocks and merge consecutive blocks with the same name."""
    cleaned_days = []
    for day in week.days:
        merged: list[ProgrammingBlock] = []
        for block in day.blocks:
            if not block.content.strip():
                continue
            if merged and merged[-1].name.lower() == block.name.lower():
                merged_instruction = "\n".join(
                    filter(None, [merged[-1].instruction, block.instruction])
                )
                merged[-1] = merged[-1].replace(
                    content=merged[-1].content + "\n" + block.content,
                    instruction=merged_instruction,
                    inter_plus=_merge_level(merged[-1], block, INTER_PLUS),
                    inter=_merge_level(merged[-1], block, INTER),
                )
            else:
                # Blocks are immutable, so the original can be shared as-is.
                merged.append(block)
        if merged:
            cleaned_days.append(
                DayProgramming(date=day.date, day_label=day.day_label, blocks=merged)
            )
    return WeeklyProgramming(week_start=week.week_start, days=cleaned_days)


def _print_prescription(lines: list[str], indent: str) -> None:
    """Print a prescription in full — the choice is unreadable when it is elided."""
    for line in lines:
        print(f"{indent}{line}" if line.strip() else "")


def _shared_lead(texts: list[list[str]]) -> int:
    """Number of leading lines every level has in common."""
    shortest = min(len(t) for t in texts)
    n = 0
    # Stop one line short: every option must still show something of its own.
    while n < shortest - 1 and len({tuple(t[: n + 1]) for t in texts}) == 1:
        n += 1
    return n


def _ask_level(block: ProgrammingBlock, day: DayProgramming) -> str:
    """Prompt for which published level of *block* to post; RX on a bare Enter."""
    levels = block.available_levels()
    print(f"\n  {day.day_label} {day.date} — {block.name}")

    # Levels usually open with the work everyone does, which would make every menu
    # line read the same. Show only what differs.
    bodies = [block.level_text(lv).splitlines() for lv in levels]
    lead = _shared_lead(bodies)
    if lead:
        print("    every level starts with:")
        _print_prescription(bodies[0][:lead], "      ")
    for i, (lv, body) in enumerate(zip(levels, bodies), start=1):
        print(f"\n    {i}) {LEVEL_LABELS[lv]}")
        _print_prescription(body[lead:], "       ")
    print()
    choices = "/".join(str(i) for i in range(1, len(levels) + 1))
    while True:
        try:
            answer = input(f"    Level? [{choices}, Enter = RX] ").strip()
        except EOFError:
            # No tty (cron, piped input): posting RX matches the pre-selection
            # behaviour, but say so rather than appearing to have asked.
            logger.warning("No input available — keeping RX for '%s'", block.name)
            return RX
        if not answer:
            return RX
        if answer.isdigit() and 1 <= int(answer) <= len(levels):
            return levels[int(answer) - 1]
        print(f"    Enter {choices}, or Enter for RX.")


def select_levels(week: WeeklyProgramming) -> WeeklyProgramming:
    """Ask which difficulty level to post for every block offering more than one.

    Single-level blocks are used as published and never prompted for. The chosen
    prescription is moved into ``content`` so every later stage (formatting,
    preview, posting) works on the selected level and nothing else.
    """
    multi = sum(len(b.available_levels()) > 1 for d in week.days for b in d.blocks)
    if not multi:
        logger.info("No block offers a scaled level this week — posting as published.")
        return week

    logger.info("%d block(s) offer more than one level — choose which to post:", multi)
    days = []
    for day in week.days:
        blocks = []
        for block in day.blocks:
            if len(block.available_levels()) == 1:
                blocks.append(block)
                continue
            level = _ask_level(block, day)
            blocks.append(block.replace(content=block.level_text(level), level=level))
        days.append(DayProgramming(date=day.date, day_label=day.day_label, blocks=blocks))
    return WeeklyProgramming(week_start=week.week_start, days=days)


def prepare_week_for_btwb(days: list[str], ws: date, relevel: bool = False) -> WeeklyProgramming:
    """Load parsed days, clean them, and LLM-format them — reusing a fresh cache.

    Both ``preview`` and ``post`` need the cleaned+formatted week, so without a
    cache the LLM formatting runs twice per ``run``. This formats only the days
    whose formatted cache is missing or stale (parsed re-analysed), reusing the
    rest, then persists the result. Output is identical to formatting every time.

    The level choice rides on that same cache: a day is only asked about when it
    is being formatted, so ``preview`` asks and ``post`` reuses the answers.
    ``relevel`` discards the cache to ask again and reformat.
    """
    cleaned = clean_week(load_days(days, ws))

    cached: dict[str, DayProgramming] = {}
    to_format: list[DayProgramming] = []
    for day in cleaned.days:
        hit = (
            None
            if relevel
            else load_formatted_day(ws, day.day_label, _parsed_source_mtime_ns(ws, day.day_label))
        )
        if hit is not None:
            logger.info("Reusing cached formatting for %s", day.day_label)
            cached[day.day_label] = hit
        else:
            to_format.append(day)

    if to_format:
        selected = select_levels(WeeklyProgramming(week_start=ws, days=to_format))
        formatted = llm_format_week(selected)
        for day in formatted.days:
            save_formatted_day(day, ws, _parsed_source_mtime_ns(ws, day.day_label))
            cached[day.day_label] = day

    # Reassemble in the cleaned order; a day with no non-empty blocks is dropped
    # exactly as llm_format_week would drop it.
    ordered = [cached[d.day_label] for d in cleaned.days if d.day_label in cached]
    return WeeklyProgramming(week_start=ws, days=ordered)


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
            level = "" if block.level == RX else f"  ({LEVEL_LABELS[block.level]})"
            logger.info("  [%s]%s", block.name, level)
            for line in block.content.splitlines():
                logger.info("      %s", line)
            if block.instruction.strip():
                logger.info("    ── coaching note ──")
                for line in block.instruction.splitlines():
                    logger.info("      %s", line)


# ── Steps ─────────────────────────────────────────────────────────────────────


def do_capture(
    days: list[str],
    no_scrcpy: bool,
    ws: date | None = None,
) -> None:
    serial = config.ANDROID_SERIAL
    ws = ws or week_start()
    scrcpy_proc = None
    reset_device_size_cache()  # fresh session — don't reuse a prior run's screen size

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

    logger.info("Capturing %d day(s) via UI text dump: %s", len(days), ", ".join(days))
    saved = 0

    for day in days:
        try:
            text = capture_day_as_text(day, serial, config.MAX_SCROLLS)
            path = save_text_capture(text, label=day, ws=ws)
            logger.info("%s saved -> %s (%d chars)", day, path.name, len(text))
            saved += 1
        except Exception as e:
            logger.error("%s: text capture failed — %s", day, e)

    if scrcpy_proc:
        scrcpy_proc.terminate()

    if saved == 0:
        logger.error("No days captured successfully")
        sys.exit(1)

    logger.info("Capture done (%d/%d days)", saved, len(days))


def _analyse_one_day(day_short: str, text: str, ws: date) -> DayProgramming:
    """Parse one day's text dump, retrying with the fallback model if blocks drop.

    Pure per-day work (no I/O), safe to run concurrently. Raises
    LLMUnavailableError if Ollama is unreachable so the caller can abort the run.
    """
    expected = len(count_block_titles(text))
    day_prog = extract_day_programming_from_text(
        text=text, day_label=day_short, target_date=short_to_date(day_short, ws)
    )
    # Retry with the fallback model when the primary returns nothing or fewer
    # blocks than the source has EMF titles (a likely dropped block).
    short = len(day_prog.blocks) < expected
    if (not day_prog.blocks or short) and config.OLLAMA_FALLBACK_TEXT_MODEL:
        logger.warning(
            "%s: primary model returned %d/%d block(s) — retrying with fallback '%s'",
            day_short,
            len(day_prog.blocks),
            expected,
            config.OLLAMA_FALLBACK_TEXT_MODEL,
        )
        fallback = extract_day_programming_from_text(
            text=text,
            day_label=day_short,
            target_date=short_to_date(day_short, ws),
            model=config.OLLAMA_FALLBACK_TEXT_MODEL,
        )
        if len(fallback.blocks) > len(day_prog.blocks):
            day_prog = fallback
    return day_prog


def analyse_days(text_captures: dict[str, str], ws: date) -> dict[str, DayProgramming | None]:
    """Parse each captured day in sequence.

    Returns {day_label: DayProgramming or None}. None marks a day whose parse
    raised (a content/parse error, already logged). LLMUnavailableError is
    propagated unchanged — it is systemic, so the caller aborts the whole run.

    Sequential by design: a single GPU serialises prefill-bound calls anyway, and
    keeping them on one Ollama slot lets the model reuse the cached instruction
    prefix between days (the parse prompt's static header is identical per day),
    which a concurrent fan-out across slots would defeat.
    """
    results: dict[str, DayProgramming | None] = {}
    for day_short, text in text_captures.items():
        try:
            results[day_short] = _analyse_one_day(day_short, text, ws)
        except LLMUnavailableError:
            raise  # systemic — abort the whole run
        except Exception as e:
            results[day_short] = None
            logger.error("%s: analysis failed — %s", day_short, e)
    return results


def do_analyse(days: list[str], ws: date | None = None) -> None:
    ws = ws or week_start()

    text_captures = load_text_captures(days, ws)

    if not text_captures:
        logger.error(
            "No captures found in %s/%s/ — run: strivee-btwb capture",
            config.CAPTURES_DIR,
            ws.isoformat(),
        )
        sys.exit(1)

    logger.info("Starting text analysis with model '%s'", config.OLLAMA_TEXT_MODEL)
    try:
        results = analyse_days(text_captures, ws)
    except LLMUnavailableError as e:
        # Systemic failure — every day would fail the same way. Abort loudly
        # rather than logging one error per day and reporting "Analysis done".
        logger.error("%s", e)
        sys.exit(1)

    # Save + tally in input order so logs stay deterministic regardless of the
    # order workers finished in.
    saved = 0
    errors = 0
    for day_short in text_captures:
        day_prog = results.get(day_short)
        if day_prog is None:
            errors += 1
        elif day_prog.blocks:
            path = save_day(day_prog, ws)
            saved += 1
            logger.info("%s cached -> %s", day_short, path.name)
        else:
            logger.warning("%s: no blocks found after fallback — skipping", day_short)

    if errors and not saved:
        # Every processed day errored: a systemic problem, not per-day noise.
        # Exit non-zero instead of printing a success line.
        logger.error("All %d day(s) failed to parse — aborting", errors)
        sys.exit(1)
    logger.info("Analysis done (%d day(s) cached)", saved)


def do_preview(days: list[str], ws: date | None = None, relevel: bool = False) -> None:
    ws = ws or week_start()
    try:
        week = prepare_week_for_btwb(days, ws, relevel)
    except LLMUnavailableError as e:
        logger.error("%s", e)
        sys.exit(1)
    if not week.days:
        logger.error("No cached analysis found — run: strivee-btwb analyse")
        sys.exit(1)
    log_summary(week)
    log_preview(week)


def _log_post_outcome(results: list[dict], ws: date) -> None:
    """Report what posted, and name anything BTWB would not take."""
    posted = [r for r in results if not r.get("skipped")]
    skipped = [r for r in results if r.get("skipped")]
    logger.info("Done — %d block(s) posted successfully", len(posted))
    if skipped:
        # BTWB's generator refuses some prescriptions outright — a hold written as
        # a time rather than reps is one shape it will not parse — and it returns
        # no preview and no error. Naming them here is the difference between work
        # to do by hand and a workout that quietly never appears on the calendar.
        logger.warning("%d block(s) BTWB would not generate — add these by hand:", len(skipped))
        for result in skipped:
            logger.warning("    %s  %s", result["date"], result["block"])
    # Posting successfully does not mean BTWB stored what was sent: its parser
    # substitutes movements it does not recognise. Kept as a separate step rather
    # than run here, so it stays re-runnable after fixing a block by hand.
    logger.info("Check what BTWB actually stored: strivee-btwb verify --week %s", ws)


def do_post(
    days: list[str],
    yes: bool,
    headless: bool,
    ws: date | None = None,
    relevel: bool = False,
) -> None:
    ws = ws or week_start()
    try:
        # Reuses preview's formatted cache when fresh, so post does not re-run the
        # LLM and does not re-ask the level choices preview already collected.
        week = prepare_week_for_btwb(days, ws, relevel)
    except LLMUnavailableError as e:
        # Abort before opening a browser / posting anything to BTWB.
        logger.error("%s", e)
        sys.exit(1)
    if not week.days:
        logger.error("No cached analysis found — run: strivee-btwb analyse")
        sys.exit(1)
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

    _log_post_outcome(results, ws)


def do_verify(days: list[str], ws: date | None = None) -> None:
    """Report where BTWB stored something other than what was posted.

    BTWB parses a posted workout with an AI that substitutes a movement it does
    not recognise rather than failing, so a block can land on the calendar
    looking fine and holding the wrong exercise. Nothing here prevents that; it
    turns a silent corruption into a line in the log.
    """
    ws = ws or week_start()
    if not config.BTWB_EMAIL or not config.BTWB_PASSWORD:
        logger.error("BTWB_EMAIL and BTWB_PASSWORD must be set in .env")
        sys.exit(1)
    try:
        week = prepare_week_for_btwb(days, ws)
    except LLMUnavailableError as e:
        logger.error("%s", e)
        sys.exit(1)
    if not week.days:
        logger.error("No cached analysis found — run: strivee-btwb analyse")
        sys.exit(1)

    dates = [d.date.isoformat() for d in week.days]
    logger.info("Reading back what BTWB stored for %d day(s)…", len(dates))
    try:
        stored = fetch_planned_workouts(config.BTWB_EMAIL, config.BTWB_PASSWORD, dates)
    except Exception as e:
        logger.error("Could not read BTWB: %s", e)
        sys.exit(1)

    logger.info("=" * 68)
    logger.info("  Posted-vs-stored check — week starting %s", ws)
    logger.info("=" * 68)
    checked = missing = 0
    mismatches: list = []
    for day in week.days:
        by_title = {_norm_title(t): body for t, body in stored[day.date.isoformat()].items()}
        for block in day.blocks:
            body = by_title.get(_norm_title(block.name))
            if body is None:
                missing += 1
                logger.warning("  %s — '%s' is not on BTWB", day.day_label, block.name)
                continue
            checked += 1
            mismatches.extend(check_stored(block.name, block.content, body))

    for mismatch in mismatches:
        logger.warning("  %s", mismatch)
    logger.info("")
    if mismatches:
        logger.info(
            "  %d line(s) across %d block(s) do not trace back to what was posted.",
            len(mismatches),
            len({m.title for m in mismatches}),
        )
        logger.info("  Fix those on BTWB by hand — the movement stored is not the one sent.")
    else:
        logger.info("  All %d posted block(s) match what was sent.", checked)
    if missing:
        logger.warning("  %d block(s) never reached BTWB — re-run post for those days.", missing)


def _confirm_delete(events: list[dict]) -> bool:
    """Prompt before deleting — list the workouts, default to No (irreversible)."""
    print(f"\nAbout to permanently delete {len(events)} workout(s) from BTWB:")
    for e in events:
        print(f"  {e['date']}  {e['title'] or '(untitled)'}")
    answer = input("Delete these? This is irreversible. [y/N] ").strip().lower()
    return answer in ("y", "yes")


def do_delete(
    days: list[str],
    yes: bool,
    headless: bool,
    ws: date | None = None,
    dry_run: bool = False,
) -> None:
    ws = ws or week_start()
    dates = [short_to_date(d, ws).isoformat() for d in days]

    if not config.BTWB_EMAIL or not config.BTWB_PASSWORD:
        logger.error("BTWB_EMAIL and BTWB_PASSWORD must be set in .env")
        sys.exit(1)

    logger.info("Deleting workouts for %s on: %s", ws, ", ".join(days))
    try:
        results = delete_week(
            email=config.BTWB_EMAIL,
            password=config.BTWB_PASSWORD,
            dates=dates,
            dry_run=dry_run,
            headless=headless,
            confirm=None if (yes or dry_run) else _confirm_delete,
        )
    except AuthenticationError as e:
        logger.error("%s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("%s", e)
        sys.exit(1)

    if dry_run:
        logger.info("Dry run — %d workout(s) would be deleted", len(results))
    else:
        logger.info("Done — %d workout(s) deleted", sum(1 for r in results if r.get("ok")))


# ── Accessory audit ───────────────────────────────────────────────────────────


def _sets_fingerprint(day: DayProgramming) -> str:
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
        formatted = load_formatted_day(ws, label, _parsed_source_mtime_ns(ws, label))
        if formatted is not None:
            ordered.append(formatted)
        elif label in parsed:
            ordered.append(parsed[label])
            fell_back.append(label)
    return WeeklyProgramming(week_start=ws, days=ordered), fell_back


def _norm_title(name: str) -> str:
    """Collapse whitespace so a title matches across our cache and BTWB's calendar.

    Block names carry the source's own spacing — "EMF 60 :  Handstand Walk" has a
    double space — and it survives into both, but comparing them raw would break
    the moment either side tidied it.
    """
    return " ".join(name.split())


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
        fingerprint = _sets_fingerprint(day)
        cached = load_sets_day(day, week.week_start, fingerprint)
        if cached is not None:
            logger.info("Reusing cached set extraction for %s", day.day_label)
            day_sets = cached
        else:
            logger.info("Extracting sets for %s (%d block(s))", day.day_label, len(day.blocks))
            day_sets = [s for block in day.blocks for s in extract_sets(block)]
            save_sets_day(day, week.week_start, fingerprint, day_sets)
        if completed is not None:
            done = {_norm_title(t) for t in completed.get(day.date.isoformat(), set())}
            skipped = {b.name for b in day.blocks if _norm_title(b.name) not in done}
            if skipped:
                logger.info(
                    "%s — not logged as done: %s", day.day_label, ", ".join(sorted(skipped))
                )
            day_sets = [s for s in day_sets if _norm_title(s.source) in done]
        collected.extend(day_sets)
    return collected


def log_audit(
    ws: date,
    volumes: dict[str, MuscleVolume],
    unlisted: list[str],
    location: str,
    fell_back: list[str],
    actual: bool = False,
    plan_ws: date | None = None,
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

    gaps = [v for v in volumes.values() if v.gap > 0]
    logger.info("")
    if not gaps:
        logger.info("  Every muscle is at target — no accessory work needed this week.")
    else:
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
    log_audit(measure_ws, volumes, unlisted, location, fell_back, actual, plan_ws)

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
