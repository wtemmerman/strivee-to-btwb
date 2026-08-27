"""Unit tests for gating the audit's set extraction on the volume it credits."""

from tests.benchmark.harness import SETS_TOLERANCE, compare_sets


def _report(volumes: dict, unlisted: list[str] | None = None) -> dict:
    return {"sets": [], "volumes": volumes, "unlisted": unlisted or []}


def test_identical_extractions_pass():
    report = _report({"chest": 3.0, "biceps": 2.0})
    assert compare_sets(report, report)["passed"]


def test_a_movement_worded_differently_passes_if_the_volume_holds():
    """The model may reword or re-split; it may not change what the week delivered."""
    base = {"sets": [{"movement": "Bench Press"}], "volumes": {"chest": 3.0}, "unlisted": []}
    now = {"sets": [{"movement": "Barbell Bench Press"}], "volumes": {"chest": 3.0}, "unlisted": []}
    assert compare_sets(base, now)["passed"]


def test_a_volume_shift_within_tolerance_passes():
    assert compare_sets(_report({"chest": 3.0}), _report({"chest": 3.0 + SETS_TOLERANCE}))["passed"]


def test_a_volume_shift_beyond_tolerance_fails():
    result = compare_sets(_report({"chest": 3.0}), _report({"chest": 4.2}))
    assert not result["passed"]
    assert result["volumes"]["chest"] == {"baseline": 3.0, "current": 4.2}


def test_a_muscle_dropping_to_zero_fails():
    """Losing a muscle entirely is the regression that would silently over-prescribe."""
    result = compare_sets(_report({"chest": 3.0}), _report({}))
    assert not result["passed"]
    assert result["volumes"]["chest"]["current"] == 0.0


def test_a_newly_unlisted_movement_fails():
    """Wording nothing credits means the week is being under-counted silently."""
    result = compare_sets(_report({}, []), _report({}, ["Zercher Carry"]))
    assert not result["passed"]
    assert result["new_unlisted"] == ["Zercher Carry"]


def test_an_already_unlisted_movement_does_not_fail_again():
    known = _report({}, ["Echo Bike"])
    assert compare_sets(known, known)["passed"]
