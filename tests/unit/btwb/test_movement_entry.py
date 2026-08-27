"""Unit tests for reading a movement-per-line block before entering it on BTWB."""

import pytest

from strivee_btwb.btwb.client import BTWBError, _parse_movement_lines
from strivee_btwb.core.models import ProgrammingBlock


def _block(content: str) -> ProgrammingBlock:
    return ProgrammingBlock(name="Accessory", content=content)


def test_each_line_becomes_a_movement_and_its_reps():
    parsed = _parse_movement_lines(_block("12 Cable Lateral Raise\n15 Seated Calf Raise"))
    assert parsed == [("Cable Lateral Raise", "12"), ("Seated Calf Raise", "15")]


def test_repeated_lines_stay_separate_sets():
    """Three rows is the point — one load field each."""
    parsed = _parse_movement_lines(_block("12 Pec Deck\n12 Pec Deck\n12 Pec Deck"))
    assert parsed == [("Pec Deck", "12")] * 3


def test_blank_lines_are_ignored():
    assert len(_parse_movement_lines(_block("12 Pec Deck\n\n  \n10 Cable Curl"))) == 2


def test_a_movement_name_may_contain_digits():
    parsed = _parse_movement_lines(_block("12 Single Arm Banded Lateral Raise"))
    assert parsed == [("Single Arm Banded Lateral Raise", "12")]


def test_an_unreadable_line_raises_rather_than_being_skipped():
    """A silently dropped set is the failure this path exists to remove."""
    with pytest.raises(BTWBError, match="Cannot parse"):
        _parse_movement_lines(_block("12 Pec Deck\n3 rounds for quality:"))


def test_a_line_with_no_rep_count_raises():
    with pytest.raises(BTWBError, match="Cannot parse"):
        _parse_movement_lines(_block("Cable Lateral Raise"))


def test_an_empty_block_raises():
    with pytest.raises(BTWBError, match="no movement lines"):
        _parse_movement_lines(_block("   \n\n"))
