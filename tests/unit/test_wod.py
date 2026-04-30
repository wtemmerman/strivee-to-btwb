"""Unit tests for WOD extraction and coaching-note stripping."""

from strivee_btwb.models import ProgrammingBlock
from strivee_btwb.wod import _extract_rx, _strip_coaching, prepare_block

# ---------------------------------------------------------------------------
# _extract_rx
# ---------------------------------------------------------------------------


def test_extract_rx_no_levels_returns_original():
    content = "3x10 Back Squat @ 70%\nRest 2 min"
    assert _extract_rx(content) == content


def test_extract_rx_returns_rx_section():
    content = "Rx : 5x5 @ 80%\nInter : 5x5 @ 65%\nInter+ : 5x5 @ 72%"
    result = _extract_rx(content)
    assert "80%" in result
    assert "65%" not in result


def test_extract_rx_no_rx_label_returns_before_inter():
    content = "5x5 @ 80%\nInter : 5x3 @ 65%"
    result = _extract_rx(content)
    assert "80%" in result
    assert "Inter" not in result


def test_extract_rx_case_insensitive():
    content = "RX - 5x5\nINTER - 3x5"
    result = _extract_rx(content)
    assert "5x5" in result
    assert "3x5" not in result


# ---------------------------------------------------------------------------
# _strip_coaching
# ---------------------------------------------------------------------------


def test_strip_coaching_keeps_workout_lines():
    content = "5x5 Back Squat @ 80%\nRest 3 min"
    assert _strip_coaching(content) == content


def test_strip_coaching_removes_objectif():
    content = "5x5 @ 80%\nObjectif : technique\nRest 3 min"
    result = _strip_coaching(content)
    assert "Objectif" not in result
    assert "5x5 @ 80%" in result
    assert "Rest 3 min" in result


def test_strip_coaching_removes_all_caps_shout():
    content = "5x5 @ 80%\nDÉPART EN CLEAN AND JERK OBLIGATOIRE AMPLITUDE COMPLÈTE!\nRest"
    result = _strip_coaching(content)
    assert "DÉPART" not in result


def test_strip_coaching_keeps_short_uppercase():
    content = "WOD\n5 Rounds"
    result = _strip_coaching(content)
    assert "WOD" in result


def test_strip_coaching_trims_trailing_blank_lines():
    content = "5x5\n\n\n"
    result = _strip_coaching(content)
    assert not result.endswith("\n")


# ---------------------------------------------------------------------------
# prepare_block
# ---------------------------------------------------------------------------


def test_prepare_block_applies_both_filters():
    content = "Rx : 5x5 @ 80%\nObjectif : stay tight\nInter : 3x5 @ 65%"
    block = ProgrammingBlock(name="Back Squat", content=content)
    result = prepare_block(block)
    assert result.name == "Back Squat"
    assert "80%" in result.content
    assert "65%" not in result.content
    assert "Objectif" not in result.content


def test_prepare_block_passthrough_when_no_levels():
    content = "21-15-9\nThrusters / Pull-ups"
    block = ProgrammingBlock(name="WOD", content=content)
    result = prepare_block(block)
    assert result.content == content
