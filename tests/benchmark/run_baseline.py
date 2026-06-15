"""Generate the accuracy + timing baseline from saved text-era captures.

Re-parses every week that has .txt captures with the CURRENT analyser, refreshes
the parsed/ cache (the committed cache may be from an older prompt), snapshots the
analyse and format outputs under baselines/, and records per-stage timing.

    python -m tests.benchmark.run_baseline

Run this AFTER any change that legitimately alters output (e.g. the num_ctx fix),
so the snapshot reflects the intended current behaviour. Later, compare.py checks
that speed changes leave these snapshots unchanged.
"""

import csv
import logging
from datetime import date

from strivee_btwb.core import config
from strivee_btwb.core.log import setup
from strivee_btwb.pipeline import clean_week, save_day

from .harness import (
    RESULTS_DIR,
    StageTiming,
    _days_to_week,
    analyse_week,
    format_fidelity,
    format_week,
    save_baseline,
    text_era_weeks,
    time_stage,
)

logger = logging.getLogger("benchmark")


def _warm() -> None:
    """Load the model once so the first timed call isn't penalised by model load."""
    from strivee_btwb.core.llm import chat_text

    chat_text("Reply with: ready", config.OLLAMA_TEXT_MODEL)


def main() -> None:
    setup(debug=False)
    weeks = text_era_weeks()
    if not weeks:
        logger.error("No text-era capture weeks found under %s", config.CAPTURES_DIR)
        raise SystemExit(1)

    logger.info("Baselining %d week(s): %s", len(weeks), ", ".join(weeks))
    _warm()

    timings: list[StageTiming] = []
    for ws in weeks:
        days, t_analyse = time_stage(analyse_week, ws, units=6, week=ws, stage="analyse")
        # Refresh parsed/ with the current parser so the cache matches the snapshot.
        for day in days:
            save_day(day, date.fromisoformat(ws))
        week = _days_to_week(ws, days)
        save_baseline("analyse", ws, week)
        timings.append(t_analyse)

        block_count = sum(len(d.blocks) for d in week.days)
        fweek, t_format = time_stage(format_week, week, units=block_count, week=ws, stage="format")
        save_baseline("format", ws, fweek)
        timings.append(t_format)

        # Surface any invented/dropped loadings in the snapshot we are about to
        # treat as ground truth, so a polluted baseline is caught at creation.
        fid = format_fidelity(clean_week(week), fweek)
        if not fid["passed"]:
            for b in fid["per_block"]:
                if b["violations"]:
                    logger.warning("%s fidelity — %s: %s", ws, b["name"], b["violations"])

        logger.info(
            "%s — analyse %.1fs (%d days), format %.1fs (%d blocks)",
            ws,
            t_analyse.seconds,
            len(week.days),
            t_format.seconds,
            block_count,
        )

    _write_csv(timings, "baseline_timing.csv")
    total = sum(t.seconds for t in timings)
    logger.info("Baseline complete — %d week(s), total LLM wall-clock %.1fs", len(weeks), total)


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
