"""Unit tests for the benchmark comparators — pure, no Ollama or captures."""

from datetime import date

from strivee_btwb.core.models import DayProgramming, ProgrammingBlock, WeeklyProgramming
from tests.benchmark.harness import (
    compare_analyse,
    compare_format,
    format_fidelity,
    format_invariants,
)

WS = date(2026, 5, 11)


def _week(blocks_by_day: dict[str, list[tuple[str, str]]]) -> WeeklyProgramming:
    days = [
        DayProgramming(
            date=WS,
            day_label=label,
            blocks=[ProgrammingBlock(name=n, content=c) for n, c in pairs],
        )
        for label, pairs in blocks_by_day.items()
    ]
    return WeeklyProgramming(week_start=WS, days=days)


# ── compare_analyse ───────────────────────────────────────────────────────────


def test_compare_analyse_identical_passes():
    w = _week({"Mon": [("Back Squat", "5x5 @ 80%"), ("WOD", "21-15-9")]})
    assert compare_analyse(w, w)["passed"] is True


def test_compare_analyse_name_set_mismatch_fails():
    base = _week({"Mon": [("Back Squat", "5x5"), ("WOD", "21-15-9")]})
    cur = _week({"Mon": [("Back Squat", "5x5")]})  # dropped a block
    report = compare_analyse(base, cur)
    assert report["passed"] is False
    assert report["per_day"][0]["names_equal"] is False


def test_compare_analyse_name_match_is_case_and_space_insensitive():
    base = _week({"Mon": [("Back  Squat", "5x5")]})
    cur = _week({"Mon": [("back squat", "5x5")]})
    assert compare_analyse(base, cur)["passed"] is True


def test_compare_analyse_low_content_similarity_fails():
    base = _week({"Mon": [("WOD", "21-15-9 Thrusters and Pull-ups for time")]})
    cur = _week({"Mon": [("WOD", "completely different content entirely unrelated text")]})
    report = compare_analyse(base, cur)
    assert report["passed"] is False
    assert report["per_day"][0]["min_ratio"] < 0.95


def test_compare_analyse_missing_day_fails():
    base = _week({"Mon": [("WOD", "x")], "Tue": [("WOD", "y")]})
    cur = _week({"Mon": [("WOD", "x")]})
    assert compare_analyse(base, cur)["passed"] is False


# ── compare_format ──────────────────────────────────────────────────────────


def test_compare_format_identical_passes():
    w = _week({"Mon": [("EMF 60 : Clean Pull", "Clean Pull\n2-2 @ 70%")]})
    assert compare_format(w, w)["passed"] is True


def test_compare_format_missing_block_fails():
    base = _week({"Mon": [("EMF 60 : A", "A\n1"), ("EMF 60 : B", "B\n2")]})
    cur = _week({"Mon": [("EMF 60 : A", "A\n1")]})
    assert compare_format(base, cur)["passed"] is False


# ── format_invariants ─────────────────────────────────────────────────────────


def test_format_invariants_clean_block_has_no_violations():
    b = ProgrammingBlock(name="EMF 60 : Clean Pull", content="Clean Pull\n2 reps @ 70%")
    assert format_invariants(b) == []


def test_format_invariants_flags_unconverted_percent():
    b = ProgrammingBlock(name="EMF 60 : Squat", content="Squat\n5 reps #80%")
    assert any("#NN%" in v or "should be @" in v for v in format_invariants(b))


def test_format_invariants_flags_unconverted_percent_range():
    b = ProgrammingBlock(name="EMF 60 : Sumo Deadlift", content="3 Sumo Deadlift #85-90% of 5RM")
    assert any("#NN%" in v or "should be @" in v for v in format_invariants(b))


def test_format_invariants_flags_unexpanded_cj():
    b = ProgrammingBlock(name="EMF 60 : Clean and Jerk", content="Clean and Jerk\n1 C&J")
    assert any("C&J" in v for v in format_invariants(b))


def test_format_invariants_allows_title_label_absent_from_content():
    # Session/category titles (not literal movements) need not appear in content.
    b = ProgrammingBlock(name="EMF 60 : CF ITW", content="For time:\n21-15-9 Thrusters")
    assert format_invariants(b) == []


def test_format_invariants_flags_empty_content():
    b = ProgrammingBlock(name="EMF 60 : Rest", content="   ")
    assert "empty content" in format_invariants(b)


# ── format_fidelity ─────────────────────────────────────────────────────────────


def test_fidelity_identical_passes():
    w = _week({"Mon": [("EMF 60 : Push Press", "@90% of your 5RM\n3 Push press")]})
    assert format_fidelity(w, w)["passed"] is True


def test_fidelity_flags_invented_load():
    # "Up to a heavy single ..." -> hallucinated "1xBW"
    src = _week({"Mon": [("EMF 60 : Clean and Jerk", "Up to a heavy single clean and jerk")]})
    out = _week({"Mon": [("EMF 60 : Clean and Jerk", "Clean and Jerk 1xBW")]})
    report = format_fidelity(src, out)
    assert report["passed"] is False
    assert any("invented" in v for v in report["per_block"][0]["violations"])


def test_fidelity_flags_dropped_rm_loading():
    # The formatter deleted the whole "#90% of your 5RM from week 1" loading line.
    src = _week({"Wed": [("EMF 60 : Push Press", "#90% of your 5RM from week 1\n3 Push press")]})
    out = _week({"Wed": [("EMF 60 : Push Press", "3 Push press")]})
    report = format_fidelity(src, out)
    assert report["passed"] is False
    assert any("dropped" in v for v in report["per_block"][0]["violations"])


def test_fidelity_hash_to_at_conversion_is_not_a_drop():
    src = _week({"Wed": [("EMF 60 : Push Press", "#90% of your 5RM from week 1\n3 Push press")]})
    out = _week({"Wed": [("EMF 60 : Push Press", "@90% of your 5RM from week 1\n3 Push press")]})
    assert format_fidelity(src, out)["passed"] is True


def test_fidelity_range_percent_conversion_is_not_invented():
    # "#80-85% of 1RM" -> "@80-85%" must not flag the low end (80) as invented.
    src = _week({"Mon": [("EMF 60 : Clean and Jerk", "1 Clean #80-85% of your 1RM")]})
    out = _week({"Mon": [("EMF 60 : Clean and Jerk", "1 Clean @80-85% of your 1RM")]})
    assert format_fidelity(src, out)["passed"] is True


def test_fidelity_ignores_loadings_below_a_level_header():
    # Sub-level scaling weights are intentionally stripped — must not flag as dropped.
    src = _week(
        {
            "Wed": [
                (
                    "EMF 60 : EST",
                    "18-12-6\nCluster @45/30kg\nINTER+ : @40/28kg\nINTER : @35/23kg",
                )
            ]
        }
    )
    out = _week({"Wed": [("EMF 60 : EST", "18-12-6\nCluster @45/30kg")]})
    assert format_fidelity(src, out)["passed"] is True


def test_fidelity_coaching_percent_line_is_not_a_loading():
    # "90% d'effort" is coaching (no RM) — removing it must not register as a drop.
    src = _week({"Fri": [("EMF 60 : Run", "On veut 90% d'effort\n25-30min easy")]})
    out = _week({"Fri": [("EMF 60 : Run", "25-30min easy")]})
    assert format_fidelity(src, out)["passed"] is True
