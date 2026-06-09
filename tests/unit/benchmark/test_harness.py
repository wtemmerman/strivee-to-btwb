"""Unit tests for the benchmark comparators — pure, no Ollama or captures."""

from datetime import date

from strivee_btwb.core.models import DayProgramming, ProgrammingBlock, WeeklyProgramming
from tests.benchmark.harness import (
    compare_analyse,
    compare_format,
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
