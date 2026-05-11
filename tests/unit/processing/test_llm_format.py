"""Unit tests for LLM-based BTWB workout formatting."""

from unittest.mock import MagicMock, patch

from strivee_btwb.core.models import ProgrammingBlock
from strivee_btwb.processing.llm_format import format_for_btwb


def _mock_response(text: str) -> MagicMock:
    msg = MagicMock()
    msg.__getitem__ = lambda self, k: text if k == "content" else None
    response = MagicMock()
    response.__getitem__ = lambda self, k: msg if k == "message" else None
    return response


@patch("strivee_btwb.processing.llm_format.ollama.chat")
def test_format_for_btwb_returns_llm_content(mock_chat):
    mock_chat.return_value = _mock_response("AMRAP 05:00\nMax sets of 5 Ring Muscle-up Unbroken")
    block = ProgrammingBlock(
        name="Gymnastics", content="AMRAP 05:00\nMax sets of 5\nINTER+\nMax sets of 3"
    )
    result = format_for_btwb(block)
    assert result.name == "Gymnastics"
    assert result.content == "AMRAP 05:00\nMax sets of 5 Ring Muscle-up Unbroken"


@patch("strivee_btwb.processing.llm_format.ollama.chat")
def test_format_for_btwb_falls_back_to_original_on_empty(mock_chat, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "OLLAMA_FORMAT_MODEL", "test-model")
    mock_chat.return_value = _mock_response("   ")
    block = ProgrammingBlock(name="Squat", content="5x5 @ 80%\nObjectif : stay tight")
    result = format_for_btwb(block)
    # Falls back to original block unchanged
    assert result.content == block.content


@patch("strivee_btwb.processing.llm_format.ollama.chat")
def test_format_for_btwb_falls_back_to_regex_on_exception(mock_chat):
    mock_chat.side_effect = RuntimeError("Ollama not running")
    block = ProgrammingBlock(name="WOD", content="21-15-9\nThrusters\nPull-ups")
    result = format_for_btwb(block)
    assert "21-15-9" in result.content


@patch("strivee_btwb.processing.llm_format.ollama.chat")
def test_format_for_btwb_converts_hash_percent_to_at(mock_chat):
    mock_chat.return_value = _mock_response(
        "Set 1 - 1 Clean and Jerk #70%\nSet 2 - 1 Clean and Jerk #75%"
    )
    block = ProgrammingBlock(name="Clean and Jerk", content="...")
    result = format_for_btwb(block)
    assert "#70%" not in result.content
    assert "@70%" in result.content
    assert "@75%" in result.content


@patch("strivee_btwb.processing.llm_format.ollama.chat")
def test_format_for_btwb_keeps_hash_on_weights(mock_chat):
    mock_chat.return_value = _mock_response("AMRAP 12:00\n6 Power clean #50/35kg\n6 Strict HSPU")
    block = ProgrammingBlock(name="WOD", content="...")
    result = format_for_btwb(block)
    assert "#50/35kg" in result.content


@patch("strivee_btwb.processing.llm_format.ollama.chat")
def test_format_for_btwb_uses_configured_model(mock_chat, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "OLLAMA_FORMAT_MODEL", "my-model")
    mock_chat.return_value = _mock_response("For time:\n21 Pull-ups")
    block = ProgrammingBlock(name="WOD", content="For time:\n21 Pull-ups")
    format_for_btwb(block)
    assert mock_chat.call_args.kwargs["model"] == "my-model"
