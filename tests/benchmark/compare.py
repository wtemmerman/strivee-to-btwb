"""Re-run the LLM stages and diff accuracy + timing against the saved baseline.

    python -m tests.benchmark.compare

Exits non-zero if ANY week's analyse or format output diverges from the baseline
beyond tolerance (block name-set must match; content similarity >= 0.95; format
invariants hold). Use after each speed change to prove accuracy is preserved and
to see the wall-clock delta.
"""

import csv
import logging

from strivee_btwb.core import config
from strivee_btwb.core.log import setup
from strivee_btwb.pipeline import load_days

from .harness import (
    RESULTS_DIR,
    StageTiming,
    _days_to_week,
    analyse_week,
    baseline_exists,
    compare_analyse,
    compare_format,
    format_week,
    load_baseline,
    text_era_weeks,
    time_stage,
)

logger = logging.getLogger("benchmark")


def _warm() -> None:
    from strivee_btwb.core.llm import chat_text

    chat_text("Reply with: ready", config.OLLAMA_TEXT_MODEL)


def main() -> None:
    setup(debug=False)
    from datetime import date

    weeks = [w for w in text_era_weeks() if baseline_exists("analyse", w)]
    if not weeks:
        logger.error("No baselines found — run: python -m tests.benchmark.run_baseline")
        raise SystemExit(1)

    logger.info("Comparing %d week(s) against baseline", len(weeks))
    _warm()

    timings: list[StageTiming] = []
    failures: list[str] = []
    base_total = cur_total = 0.0

    for ws in weeks:
        days, t_analyse = time_stage(analyse_week, ws, units=6, week=ws, stage="analyse")
        cur_week = _days_to_week(ws, days)
        a_report = compare_analyse(load_baseline("analyse", ws), cur_week)
        if not a_report["passed"]:
            failures.append(f"{ws} analyse: {a_report['per_day']}")

        parsed_week = load_days(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"], date.fromisoformat(ws))
        block_count = sum(len(d.blocks) for d in parsed_week.days)
        fweek, t_format = time_stage(
            format_week, parsed_week, units=block_count, week=ws, stage="format"
        )
        f_report = compare_format(load_baseline("format", ws), fweek)
        if not f_report["passed"]:
            bad = [b for b in f_report["per_block"] if b.get("violations") or b["ratio"] < 0.95]
            failures.append(f"{ws} format: {bad}")

        timings.extend([t_analyse, t_format])
        status = "OK" if a_report["passed"] and f_report["passed"] else "FAIL"
        logger.info(
            "%s — analyse %.1fs / format %.1fs — accuracy %s",
            ws,
            t_analyse.seconds,
            t_format.seconds,
            status,
        )

    _write_csv(timings, "compare_timing.csv")
    cur_total = sum(t.seconds for t in timings)
    base_total = _baseline_total()
    if base_total:
        logger.info(
            "Wall-clock: baseline %.1fs → now %.1fs (%.1fx)",
            base_total,
            cur_total,
            base_total / cur_total if cur_total else 0,
        )

    if failures:
        logger.error("ACCURACY REGRESSION in %d week/stage(s):", len(failures))
        for f in failures:
            logger.error("  %s", f)
        raise SystemExit(1)
    logger.info("All weeks match baseline within tolerance — accuracy preserved.")


def _baseline_total() -> float:
    path = RESULTS_DIR / "baseline_timing.csv"
    if not path.exists():
        return 0.0
    with path.open() as f:
        return sum(float(row["seconds"]) for row in csv.DictReader(f))


def _write_csv(timings: list[StageTiming], name: str) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / name
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["week", "stage", "seconds", "units"])
        for t in timings:
            w.writerow([t.week, t.stage, f"{t.seconds:.3f}", t.units])
    logger.info("Timing written to %s", path)


if __name__ == "__main__":
    main()
