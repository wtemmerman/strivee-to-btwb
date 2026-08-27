"""Core benchmark logic: stage runners, snapshot I/O, and accuracy comparators.

Split out from the CLI entry points so the pure comparator functions (no Ollama,
no filesystem) can be unit-tested in tests/unit/benchmark/.
"""

from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from strivee_btwb.core import config
from strivee_btwb.core.models import DayProgramming, ProgrammingBlock, WeeklyProgramming
from strivee_btwb.pipeline import (
    analyse_days,
    clean_week,
    llm_format_week,
    load_text_captures,
)
from strivee_btwb.processing import extract_sets
from strivee_btwb.processing.volume import weekly_volume

# Accuracy gate: a per-block content similarity below this fails the comparison.
CONTENT_RATIO_THRESHOLD = 0.95

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

_BENCH_DIR = Path(__file__).parent
BASELINE_DIR = _BENCH_DIR / "baselines"
RESULTS_DIR = _BENCH_DIR / "results"


# ── week discovery ────────────────────────────────────────────────────────────


def text_era_weeks() -> list[str]:
    """Return week folders (ISO Monday) that have .txt captures, newest first.

    PNG-only (vision-era) weeks are skipped — they cannot exercise the text
    analyser, so they are not valid ground truth for this benchmark.
    """
    weeks = []
    for wk in sorted(config.CAPTURES_DIR.glob("*/")):
        if any(wk.glob("strivee_*.txt")):
            weeks.append(wk.name)
    return weeks


# ── stage runners (these exercise the real pipeline code paths) ────────────────


@dataclass
class StageTiming:
    week: str
    stage: str
    seconds: float
    units: int  # days parsed or blocks formatted


def analyse_week(ws_iso: str, days: list[str] | None = None) -> list[DayProgramming]:
    """Re-parse a week's saved text captures via the real parallel analyse core.

    Uses pipeline.analyse_days so timing reflects the concurrent path and the
    comparison proves the parallel core yields the same per-day output as the
    sequential baseline. Returns days in stable label order.
    """
    ws = date.fromisoformat(ws_iso)
    days = days or _DAYS
    captures = load_text_captures(days, ws)
    results = analyse_days(captures, ws)
    return [results[d] for d in days if results.get(d) is not None]


def format_week(week: WeeklyProgramming) -> WeeklyProgramming:
    """Run the real clean + LLM-format pipeline stage (timed by callers)."""
    return llm_format_week(clean_week(week))


def time_stage(fn, *args, units: int, week: str, stage: str) -> tuple[object, StageTiming]:
    start = time.perf_counter()
    result = fn(*args)
    return result, StageTiming(week, stage, time.perf_counter() - start, units)


# ── snapshot (baseline) I/O ────────────────────────────────────────────────────


def _week_to_dict(week: WeeklyProgramming) -> dict:
    return {
        "week_start": week.week_start.isoformat(),
        "days": [
            {
                "date": d.date.isoformat(),
                "day_label": d.day_label,
                "blocks": [
                    {
                        "name": b.name,
                        "content": b.content,
                        "instruction": b.instruction,
                        "inter_plus": b.inter_plus,
                        "inter": b.inter,
                    }
                    for b in d.blocks
                ],
            }
            for d in week.days
        ],
    }


def _days_to_week(ws_iso: str, days: list[DayProgramming]) -> WeeklyProgramming:
    return WeeklyProgramming(week_start=date.fromisoformat(ws_iso), days=days)


def save_baseline(kind: str, ws_iso: str, week: WeeklyProgramming) -> Path:
    """Persist an analyse/format snapshot for one week under baselines/<kind>/."""
    out = BASELINE_DIR / kind
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{ws_iso}.json"
    path.write_text(json.dumps(_week_to_dict(week), indent=2, ensure_ascii=False))
    return path


def load_baseline(kind: str, ws_iso: str) -> WeeklyProgramming:
    """Load a previously saved analyse/format snapshot for one week."""
    path = BASELINE_DIR / kind / f"{ws_iso}.json"
    data = json.loads(path.read_text())
    days = [
        DayProgramming(
            date=date.fromisoformat(d["date"]),
            day_label=d["day_label"],
            blocks=[
                ProgrammingBlock(
                    name=b["name"],
                    content=b["content"],
                    instruction=b.get("instruction", ""),
                    inter_plus=b.get("inter_plus", ""),
                    inter=b.get("inter", ""),
                )
                for b in d["blocks"]
            ],
        )
        for d in data["days"]
    ]
    return WeeklyProgramming(week_start=date.fromisoformat(data["week_start"]), days=days)


def baseline_exists(kind: str, ws_iso: str) -> bool:
    return (BASELINE_DIR / kind / f"{ws_iso}.json").exists()


# ── set extraction (audit stage) ──────────────────────────────────────────────

SETS_TOLERANCE = 0.5
"""How far a muscle's credited volume may move before the extraction is a regression.

The gate is the volume, not the set list, because the volume is what the audit
acts on. The model may legitimately reword a movement or split a block
differently; it may not change what the week is judged to have delivered. Half a
set is below the resolution of any prescription — gaps round up to whole sets —
so anything larger would change the advice.
"""


def sets_week(week: WeeklyProgramming) -> dict:
    """Extract every block's sets and the per-muscle volume they credit."""
    work_sets = [s for day in week.days for block in day.blocks for s in extract_sets(block)]
    volumes, unlisted = weekly_volume(work_sets)
    return {
        "sets": [
            {
                "movement": s.movement,
                "sets": s.sets,
                "reps": s.reps,
                "block_type": s.block_type,
                "source": s.source,
            }
            for s in work_sets
        ],
        "volumes": {m: round(v.credited, 3) for m, v in volumes.items()},
        "unlisted": sorted(unlisted),
    }


def save_sets_baseline(ws_iso: str, report: dict) -> Path:
    """Persist a set-extraction snapshot for one week under baselines/sets/."""
    out = BASELINE_DIR / "sets"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{ws_iso}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return path


def load_sets_baseline(ws_iso: str) -> dict:
    return json.loads((BASELINE_DIR / "sets" / f"{ws_iso}.json").read_text())


def compare_sets(baseline: dict, current: dict) -> dict:
    """Compare two set-extraction snapshots by the volume they credit.

    A movement the table does not know is a failure rather than a note: it means
    the extraction started producing wording nothing credits, so the week is
    being under-counted silently. Add it to movement_muscles.json and re-baseline.
    """
    moved = {}
    for muscle, base in baseline["volumes"].items():
        now = current["volumes"].get(muscle, 0.0)
        if abs(now - base) > SETS_TOLERANCE:
            moved[muscle] = {"baseline": base, "current": now}
    appeared = sorted(set(current["unlisted"]) - set(baseline["unlisted"]))
    return {"passed": not moved and not appeared, "volumes": moved, "new_unlisted": appeared}


# ── pure comparators (unit-tested without Ollama) ──────────────────────────────


def _norm_name(s: str) -> str:
    return " ".join(s.split()).casefold()


def _norm_content(s: str) -> str:
    return "\n".join(line.rstrip() for line in s.strip().splitlines())


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm_content(a), _norm_content(b)).ratio()


def _index_blocks(day: DayProgramming) -> dict[str, ProgrammingBlock]:
    return {_norm_name(b.name): b for b in day.blocks}


def compare_analyse(baseline: WeeklyProgramming, current: WeeklyProgramming) -> dict:
    """Compare two analyse results: block name-sets and per-block content ratio.

    Passes when every day has the same set of block names (case/space-insensitive)
    and every matched block's content similarity >= CONTENT_RATIO_THRESHOLD.
    """
    base_days = {d.day_label: d for d in baseline.days}
    cur_days = {d.day_label: d for d in current.days}
    per_day = []
    ok = True
    for label in sorted(set(base_days) | set(cur_days)):
        bd, cd = base_days.get(label), cur_days.get(label)
        if bd is None or cd is None:
            per_day.append(
                {"day": label, "names_equal": False, "min_ratio": 0.0, "note": "missing"}
            )
            ok = False
            continue
        bnames, cnames = set(_index_blocks(bd)), set(_index_blocks(cd))
        names_equal = bnames == cnames
        ratios = [
            _ratio(_index_blocks(bd)[n].content, _index_blocks(cd)[n].content)
            for n in (bnames & cnames)
        ]
        min_ratio = min(ratios) if ratios else 1.0
        day_ok = names_equal and min_ratio >= CONTENT_RATIO_THRESHOLD
        ok = ok and day_ok
        per_day.append(
            {
                "day": label,
                "names_equal": names_equal,
                "min_ratio": round(min_ratio, 4),
                "base_n": len(bnames),
                "cur_n": len(cnames),
            }
        )
    return {"passed": ok, "per_day": per_day}


def format_invariants(block: ProgrammingBlock) -> list[str]:
    """Return a list of invariant violations for one formatted block (empty = OK).

    Checks only properties the formatter actually guarantees: the post-processing
    regexes (#NN% -> @NN%, C&J expansion) must have run, and content must be
    non-empty. Note: we deliberately do NOT require the title's movement label to
    appear in the content — many EMF titles are session/category names (e.g.
    "CF ITW", "Energy system training specific") that never appear verbatim in the
    prescription, so such a check fires on byte-identical, correct output.
    """
    violations = []
    if not block.content.strip():
        violations.append("empty content")
    if re.search(r"#\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?%", block.content):
        violations.append("unconverted #NN% (should be @NN%)")
    if re.search(r"\bC\s*&\s*J\b", block.content, re.IGNORECASE):
        violations.append("unexpanded C&J")
    return violations


# Athlete-level sub-section headers. Content from the first such line onward is
# intentionally stripped by the formatter, so loadings below it are not required.
_LEVEL_HEADER_RE = re.compile(
    r"^[^\w@#]*(RX|INTER\+?|SCALED|ELITE|D[ÉE]BUTANT|BEGINNER|INTERMEDIATE)\b",
    re.IGNORECASE,
)


def _load_numbers(text: str) -> set[str]:
    """Numbers that are part of a LOAD token: %, kg, lb, RM, @N, or N x BW.

    Used to detect invented loads. Reps/cals/distances (numbers not adjacent to a
    load unit) are deliberately excluded so normal prescriptions don't register.
    The #NN% -> @NN% normalisation matches the formatter's own post-processing.
    """
    norm = re.sub(r"#(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)%", r"@\1%", text)
    nums: set[str] = set()
    for pat in (
        r"(\d+(?:\.\d+)?)\s*%",
        r"(\d+(?:\.\d+)?)\s*(?:kg|lb)\b",
        r"@\s*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*RM\b",
        r"(\d+(?:\.\d+)?)\s*x?\s*BW\b",
    ):
        nums.update(re.findall(pat, norm, re.IGNORECASE))
    return nums


def _pre_level_loading_percents(text: str) -> set[str]:
    """Percentage values from RM-loading lines that precede any level header.

    These are top-level prescription loadings the formatter must keep. Restricted
    to lines that carry both a '%' and 'RM' so coaching lines like '90% d'effort'
    (correctly removed) never register as a dropped loading.
    """
    out: set[str] = set()
    for line in text.splitlines():
        if _LEVEL_HEADER_RE.match(line.strip()):
            break
        if re.search(r"\d+\s*%", line) and re.search(r"RM\b", line, re.IGNORECASE):
            out.update(re.findall(r"(\d+(?:\.\d+)?)\s*%", line))
    return out


def format_fidelity(source: WeeklyProgramming, formatted: WeeklyProgramming) -> dict:
    """Source-grounded format checks, independent of the (same-model) baseline.

    For each block matched by name between the formatter's INPUT (cleaned analyse
    output) and its OUTPUT, flags:
      A. invented load numbers — a %/kg/lb/RM/BW number in the output absent from
         the input (catches hallucinations like 'Up to a heavy single' -> '1xBW');
      B. dropped RM-loadings — a percentage on a pre-level-header RM-loading line
         in the input that does not survive into the output (catches the formatter
         silently deleting '#90% of your 5RM from week 1').
    Both directions are safe against the formatter's legitimate removals: stripping
    sub-level sections only deletes content (never adds numbers), and dropped-load
    detection ignores anything at or below the first athlete-level header.
    """
    s_days = {d.day_label: _index_blocks(d) for d in source.days}
    f_days = {d.day_label: _index_blocks(d) for d in formatted.days}
    per_block = []
    ok = True
    for label in sorted(set(s_days) | set(f_days)):
        sidx, fidx = s_days.get(label, {}), f_days.get(label, {})
        for name in sorted(set(sidx) & set(fidx)):
            s_content, f_content = sidx[name].content, fidx[name].content
            f_loads = _load_numbers(f_content)
            hallucinated = sorted(f_loads - _load_numbers(s_content))
            dropped = sorted(_pre_level_loading_percents(s_content) - f_loads)
            violations = []
            if hallucinated:
                violations.append(f"invented load number(s): {hallucinated}")
            if dropped:
                violations.append(f"dropped RM-loading percent(s): {dropped}")
            ok = ok and not violations
            per_block.append({"day": label, "name": sidx[name].name, "violations": violations})
    return {"passed": ok, "per_block": per_block}


def compare_format(baseline: WeeklyProgramming, current: WeeklyProgramming) -> dict:
    """Compare two format results: per-block content ratio + invariant checks."""
    base_days = {d.day_label: d for d in baseline.days}
    cur_days = {d.day_label: d for d in current.days}
    per_block = []
    ok = True
    for label in sorted(set(base_days) | set(cur_days)):
        bd, cd = base_days.get(label), cur_days.get(label)
        if bd is None or cd is None:
            ok = False
            per_block.append({"day": label, "name": "*", "ratio": 0.0, "note": "missing day"})
            continue
        bidx, cidx = _index_blocks(bd), _index_blocks(cd)
        for name in sorted(set(bidx) | set(cidx)):
            if name not in bidx or name not in cidx:
                ok = False
                per_block.append(
                    {"day": label, "name": name, "ratio": 0.0, "note": "missing block"}
                )
                continue
            ratio = _ratio(bidx[name].content, cidx[name].content)
            violations = format_invariants(cidx[name])
            block_ok = ratio >= CONTENT_RATIO_THRESHOLD and not violations
            ok = ok and block_ok
            per_block.append(
                {
                    "day": label,
                    "name": cidx[name].name,
                    "ratio": round(ratio, 4),
                    "violations": violations,
                }
            )
    return {"passed": ok, "per_block": per_block}
