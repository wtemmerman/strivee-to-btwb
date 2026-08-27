"""Unit tests for catching movements BTWB substituted for the ones that were sent.

Every case here is a real posting from a live week, so the thresholds are pinned
against what BTWB actually does rather than against invented examples.
"""

from strivee_btwb.processing.movement_check import check_stored, source_vocabulary


def _flagged(source: str, stored: str) -> list[str]:
    return [m.stored for m in check_stored("EMF 60 : Block", source, stored)]


# ── Substitutions seen on real weeks ──────────────────────────────────────────


def test_a_wholly_invented_movement_is_flagged():
    assert _flagged(
        "EMOMx9 :\nmin 1 to 3 - 3 Weighted Strict Chest-to-Ring Pull-up",
        "3 Burpee Box Jump Over + 12/9 Muscle Ups",
    ) == ["3 Burpee Box Jump Over + 12/9 Muscle Ups"]


def test_a_half_right_movement_is_flagged():
    """'Clean and jerk' stored as 'Snatch Balance+1' shares only the word Snatch."""
    assert _flagged(
        "6 sets of :\n2 Clean and jerk #50/35kg\n2 Snatch #50/35kg", "2 Snatch Balance+1"
    )


def test_a_block_replaced_entirely_is_flagged():
    assert _flagged(
        "Strict Bar Muscle-up\nAccumulated 6-8 Reps Banded Bar Muscle-up",
        "8 Clean Pull + Cleans",
    ) == ["8 Clean Pull + Cleans"]


def test_only_lines_with_a_rep_count_are_checked():
    """The event page mixes prescriptions with form labels; the count tells them apart."""
    assert _flagged("12 Ring Dip", "Schéma de répétitions\nPoids par série\nAssigner") == []


def test_a_movement_stored_without_a_rep_count_goes_unchecked():
    """A known blind spot, accepted so the report stays worth reading."""
    assert _flagged("For time :\n2000m Row", "Echo Bike Calorie") == []


# ── Rewordings BTWB is entitled to make ───────────────────────────────────────


def test_an_expanded_abbreviation_is_not_a_substitution():
    assert _flagged("2 Strict HSPU / Abmat strict HSPU", "2 Strict Handstand Push-ups") == []


def test_a_pluralised_movement_is_not_a_substitution():
    assert _flagged("2 Snatch #50/35kg", "2 Snatches") == []
    assert _flagged("12 Lateral Raise", "12 Lateral Raises") == []


def test_a_reordered_line_is_not_a_substitution():
    assert _flagged("5RM Barbell bench press", "Barbell Bench Press : 5 Rep Max") == []


def test_units_and_rep_schemes_do_not_count_against_a_line():
    assert _flagged("For time :\n2000m Row", "Row, 2000 m") == []
    assert _flagged("For time :\n50 Strict Pull-up", "50 Strict Pull-ups") == []


def test_the_echoed_title_is_not_checked():
    """BTWB prints the block title above the body; it is not a movement."""
    assert (
        check_stored(
            "EMF 60 : Bench press",
            "5RM Barbell bench press",
            "EMF 60 : Bench press\n\nBarbell Bench Press : 5 Rep Max",
        )
        == []
    )


# ── Vocabulary ────────────────────────────────────────────────────────────────


def test_abbreviations_expand_on_the_source_side():
    assert {"handstand", "push", "up"} <= source_vocabulary("2 Strict HSPU")


def test_units_are_not_vocabulary():
    assert "reps" not in source_vocabulary("12 reps Ring Dip")
    assert "ring" in source_vocabulary("12 reps Ring Dip")


def test_a_clean_block_reports_nothing():
    assert check_stored("T", "For time :\n50 Strict Pull-up", "50 Strict Pull-ups") == []
