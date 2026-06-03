"""Unit tests for LLM-based BTWB workout formatting."""

from unittest.mock import MagicMock, patch

import pytest

from strivee_btwb.core.llm import LLMUnavailableError
from strivee_btwb.core.models import ProgrammingBlock
from strivee_btwb.processing.llm_format import (
    _ensure_movement_in_content,
    _movement_from_block_name,
    format_for_btwb,
)


def _mock_response(text: str) -> MagicMock:
    msg = MagicMock()
    msg.__getitem__ = lambda self, k: text if k == "content" else None
    response = MagicMock()
    response.__getitem__ = lambda self, k: msg if k == "message" else None
    return response


@patch("strivee_btwb.core.llm.ollama.chat")
def test_format_for_btwb_returns_llm_content(mock_chat):
    mock_chat.return_value = _mock_response("AMRAP 05:00\nMax sets of 5 Ring Muscle-up Unbroken")
    block = ProgrammingBlock(
        name="Gymnastics", content="AMRAP 05:00\nMax sets of 5\nINTER+\nMax sets of 3"
    )
    result = format_for_btwb(block)
    assert result.name == "Gymnastics"
    assert result.content == "AMRAP 05:00\nMax sets of 5 Ring Muscle-up Unbroken"


@patch("strivee_btwb.core.llm.ollama.chat")
def test_format_for_btwb_falls_back_to_original_on_empty(mock_chat, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "OLLAMA_FORMAT_MODEL", "test-model")
    mock_chat.return_value = _mock_response("   ")
    block = ProgrammingBlock(name="Squat", content="5x5 @ 80%\nObjectif : stay tight")
    result = format_for_btwb(block)
    # Falls back to original block unchanged
    assert result.content == block.content


@patch("strivee_btwb.core.llm.ollama.chat")
def test_format_for_btwb_raises_when_ollama_unavailable(mock_chat):
    # A failed model call must abort (fail loud), NOT silently return the raw
    # unformatted block — otherwise raw Strivee text gets posted to BTWB.
    mock_chat.side_effect = RuntimeError("Ollama not running")
    block = ProgrammingBlock(name="WOD", content="21-15-9\nThrusters\nPull-ups")
    with pytest.raises(LLMUnavailableError):
        format_for_btwb(block)


@patch("strivee_btwb.core.llm.ollama.chat")
def test_format_for_btwb_converts_hash_percent_to_at(mock_chat):
    mock_chat.return_value = _mock_response(
        "Set 1 - 1 Clean and Jerk #70%\nSet 2 - 1 Clean and Jerk #75%"
    )
    block = ProgrammingBlock(name="Clean and Jerk", content="...")
    result = format_for_btwb(block)
    assert "#70%" not in result.content
    assert "@70%" in result.content
    assert "@75%" in result.content


@patch("strivee_btwb.core.llm.ollama.chat")
def test_format_for_btwb_keeps_hash_on_weights(mock_chat):
    mock_chat.return_value = _mock_response("AMRAP 12:00\n6 Power clean #50/35kg\n6 Strict HSPU")
    block = ProgrammingBlock(name="WOD", content="...")
    result = format_for_btwb(block)
    assert "#50/35kg" in result.content


# ---------------------------------------------------------------------------
# _movement_from_block_name
# ---------------------------------------------------------------------------


def test_movement_from_block_name_colon_separator():
    assert _movement_from_block_name("EMF 60 : Clean Pull") == "Clean Pull"


def test_movement_from_block_name_dash_separator():
    result = _movement_from_block_name("EMF 60 - Gymnastic Ring Muscle-up")
    assert result == "Gymnastic Ring Muscle-up"


def test_movement_from_block_name_rx_prefix():
    assert _movement_from_block_name("EMF RX : Gym - Maintenance") == "Gym - Maintenance"


def test_movement_from_block_name_no_match_returns_none():
    assert _movement_from_block_name("Back Squat") is None
    assert _movement_from_block_name("WOD") is None


# ---------------------------------------------------------------------------
# _ensure_movement_in_content
# ---------------------------------------------------------------------------


def test_ensure_movement_prepends_when_absent():
    block = ProgrammingBlock(
        name="EMF 60 : Clean Pull",
        content="3 reps @100-105% of your 1RM Clean and Jerk",
        instruction="",
    )
    result = _ensure_movement_in_content(block)
    assert result.content.startswith("Clean Pull\n")


def test_ensure_movement_skips_when_already_present():
    block = ProgrammingBlock(
        name="EMF 60 : Push Press",
        content="EMOMx6:\n1 Push press\n#95% of your 5RM",
        instruction="",
    )
    result = _ensure_movement_in_content(block)
    assert result.content == block.content  # unchanged


def test_ensure_movement_case_insensitive_check():
    block = ProgrammingBlock(
        name="EMF 60 : Snatch",
        content="Build to a 1RM squat snatch for the day",
        instruction="",
    )
    result = _ensure_movement_in_content(block)
    assert result.content == block.content  # "snatch" found case-insensitively


def test_ensure_movement_no_emf_prefix_unchanged():
    block = ProgrammingBlock(name="WOD", content="AMRAP 10:00\n5 Pull-ups", instruction="")
    result = _ensure_movement_in_content(block)
    assert result.content == block.content


# ---------------------------------------------------------------------------
# C&J → Clean and Jerk substitution
# ---------------------------------------------------------------------------


@patch("strivee_btwb.core.llm.ollama.chat")
def test_format_replaces_cj_abbreviation(mock_chat):
    mock_chat.return_value = _mock_response("1 C&J @85%")
    block = ProgrammingBlock(name="EMF 60 : Clean and Jerk", content="1 C&J @85%")
    result = format_for_btwb(block)
    assert "C&J" not in result.content
    assert "Clean and Jerk" in result.content


@patch("strivee_btwb.core.llm.ollama.chat")
def test_format_replaces_cj_case_insensitive(mock_chat):
    mock_chat.return_value = _mock_response("1 c&j @85%")
    block = ProgrammingBlock(name="EMF 60 : Clean and Jerk", content="1 c&j @85%")
    result = format_for_btwb(block)
    assert "Clean and Jerk" in result.content


@patch("strivee_btwb.core.llm.ollama.chat")
def test_format_for_btwb_uses_configured_model(mock_chat, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "OLLAMA_FORMAT_MODEL", "my-model")
    mock_chat.return_value = _mock_response("For time:\n21 Pull-ups")
    block = ProgrammingBlock(name="WOD", content="For time:\n21 Pull-ups")
    format_for_btwb(block)
    assert mock_chat.call_args.kwargs["model"] == "my-model"
