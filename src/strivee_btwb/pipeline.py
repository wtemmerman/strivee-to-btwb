"""
Orchestration pipeline: cache I/O, week processing, and step implementations.

Each step (capture, analyse, preview, post) is a standalone function that reads
from the previous step's cache, so steps can be run independently or restarted.
"""

import logging
import re
import sys
from datetime import date, timedelta

import ollama

from .btwb import (
    AuthenticationError,
    delete_week,
    fetch_planned_workouts,
    post_week,
)
from .cache import (
    load_days,
    load_formatted_day,
    load_text_captures,
    parsed_source_mtime_ns,
    save_day,
    save_formatted_day,
    save_text_capture,
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
from .core.btwb_names import confirmed_movements
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
from .processing import format_for_btwb
from .processing.erg_intervals import describe, erg_intervals
from .processing.lift_sets import (
    alternating_emom,
    classic_sets,
    describe_alternating,
    describe_sets,
)
from .processing.movement_check import check_stored
from .processing.plus_split import is_lead_in, split_plus_joins
from .vision import count_block_titles, extract_day_programming_from_text

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


# ── Week processing ───────────────────────────────────────────────────────────


def split_week(week: WeeklyProgramming) -> WeeklyProgramming:
    """Split every "+"-joined block into the separate workouts it is posted as."""
    return WeeklyProgramming(
        week_start=week.week_start,
        days=[
            DayProgramming(
                date=day.date,
                day_label=day.day_label,
                blocks=[part for b in day.blocks for part in split_plus_joins(b)],
            )
            for day in week.days
        ],
    )


def llm_format_week(week: WeeklyProgramming) -> WeeklyProgramming:
    """Split "+"-joined blocks, then LLM-format every workout for BTWB.

    The split runs after level selection because each level can join its parts
    differently, and before formatting so each part is formatted on its own.

    Kept sequential on purpose: on a single GPU these calls are prefill-bound, so
    running them concurrently contends for the GPU and is slower, not faster
    (measured ~5x slower for 4-way analyse concurrency). The real cost is removed
    by caching the formatted week (see prepare_week_for_btwb) so it runs once.
    """
    days = []
    try:
        for day in split_week(week).days:
            logger.info("Formatting %s with LLM…", day.day_label)
            blocks = [_format_block(b) for b in day.blocks]
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


def _note_with_prescription(block: ProgrammingBlock) -> str:
    """The note: the prescription as Strivee wrote it, then the coach's notes.

    A warm-up or drill list the split moved into the note stays ahead of the
    prescription, in the order the session is done.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", block.instruction) if p.strip()]
    lead = 0
    while lead < len(paragraphs) and is_lead_in(paragraphs[lead]):
        lead += 1
    ordered = paragraphs[:lead] + [block.content.strip()] + paragraphs[lead:]
    return "\n\n".join(p for p in ordered if p)


def _format_block(block: ProgrammingBlock) -> ProgrammingBlock:
    """Format one workout: an erg interval set for the classic builder, else via the LLM.

    Either way BTWB keeps the structure and drops detail — an erg's watts, a lift's
    RPE or tempo — so the note opens with the prescription as Strivee wrote it.
    """
    note = _note_with_prescription(block)
    if (plan := erg_intervals(block)) is not None:
        return block.replace(content=describe(plan), instruction=note, erg=plan)
    if (sets := classic_sets(block)) is not None:
        return block.replace(content=describe_sets(sets), instruction=note, sets=sets)
    if (turns := alternating_emom(block)) is not None:
        return block.replace(
            content=describe_alternating(turns), instruction=note, alternating=turns
        )
    return format_for_btwb(block).replace(instruction=note)


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


class LevelChoiceNeededError(RuntimeError):
    """A multi-level block has no cached level choice and nobody can be asked."""


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
            # No terminal to ask (cron, piped input, a post after a cache-version
            # bump). Falling back to RX posted the wrong level without a word.
            raise LevelChoiceNeededError(
                f"{day.day_label} '{block.name}' needs a level choice and there is no "
                f"terminal to ask. Run in a terminal: strivee-btwb preview "
                f"--week {day.date.isoformat()} --days {day.day_label} --relevel"
            ) from None
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
            else load_formatted_day(ws, day.day_label, parsed_source_mtime_ns(ws, day.day_label))
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
            save_formatted_day(day, ws, parsed_source_mtime_ns(ws, day.day_label))
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


def _unconfirmed_movements(block: ProgrammingBlock) -> list[str]:
    """The movements a classic block needs that BTWB has not been seen to hold."""
    needed = [plan.movement for plan in (block.erg, block.sets) if plan is not None]
    if block.alternating is not None:
        needed += block.alternating.movements
    return [m for m in dict.fromkeys(needed) if m not in confirmed_movements()]


def log_preview(week: WeeklyProgramming) -> list[str]:
    """Print what will be posted; return the movement names BTWB has not confirmed."""
    unconfirmed: list[str] = []
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
            for movement in _unconfirmed_movements(block):
                unconfirmed.append(movement)
                logger.warning(
                    "    BTWB has not been seen to hold a movement named '%s' — if it has "
                    "none, post skips this block",
                    movement,
                )
    return unconfirmed


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


def _prepared_week_or_exit(days: list[str], ws: date, relevel: bool = False) -> WeeklyProgramming:
    """The formatted week preview, post and verify work on — or exit before BTWB is touched."""
    try:
        week = prepare_week_for_btwb(days, ws, relevel)
    except (LLMUnavailableError, LevelChoiceNeededError) as e:
        logger.error("%s", e)
        sys.exit(1)
    if not week.days:
        logger.error("No cached analysis found — run: strivee-btwb analyse")
        sys.exit(1)
    return week


def do_preview(days: list[str], ws: date | None = None, relevel: bool = False) -> list[str]:
    """Show the week as it will be posted; return the movement names BTWB has not confirmed."""
    ws = ws or week_start()
    week = _prepared_week_or_exit(days, ws, relevel)
    log_summary(week)
    return log_preview(week)


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
    # Reuses preview's formatted cache when fresh, so post does not re-run the
    # LLM and does not re-ask the level choices preview already collected.
    week = _prepared_week_or_exit(days, ws, relevel)
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
    week = _prepared_week_or_exit(days, ws)

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
        by_title = {norm_title(t): body for t, body in stored[day.date.isoformat()].items()}
        for block in day.blocks:
            body = by_title.get(norm_title(block.name))
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
    titles: frozenset[str] | None = None,
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
            titles=titles,
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


def norm_title(name: str) -> str:
    """Collapse whitespace so a title matches across our cache and BTWB's calendar.

    Block names carry the source's own spacing — "EMF 60 :  Handstand Walk" has a
    double space — and it survives into both, but comparing them raw would break
    the moment either side tidied it.
    """
    return " ".join(name.split())
