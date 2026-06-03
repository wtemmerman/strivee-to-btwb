"""Unit tests for the centralised Ollama wrapper (core/llm.py)."""

from unittest.mock import patch

import httpx
import ollama
import pytest

from strivee_btwb.core.llm import LLMUnavailableError, chat_json, chat_text


def _response(text: str) -> dict:
    return {"message": {"content": text}}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_chat_text_returns_content():
    with patch("strivee_btwb.core.llm.ollama.chat", return_value=_response("hello")):
        assert chat_text("prompt", "model") == "hello"


def test_chat_text_passes_prompt_and_model():
    with patch("strivee_btwb.core.llm.ollama.chat", return_value=_response("x")) as mock_chat:
        chat_text("my prompt", "my-model")
        kwargs = mock_chat.call_args.kwargs
        assert kwargs["model"] == "my-model"
        assert kwargs["messages"][0]["content"] == "my prompt"
        assert kwargs["think"] is False
        # No format requested for plain text calls.
        assert "format" not in kwargs


def test_chat_json_with_schema_sets_format():
    schema = {"type": "object"}
    with patch("strivee_btwb.core.llm.ollama.chat", return_value=_response("{}")) as mock_chat:
        chat_json("prompt", "model", schema=schema)
        assert mock_chat.call_args.kwargs["format"] == schema


def test_chat_json_without_schema_requests_json_mode():
    with patch("strivee_btwb.core.llm.ollama.chat", return_value=_response("{}")) as mock_chat:
        chat_json("prompt", "model")
        assert mock_chat.call_args.kwargs["format"] == "json"


# ---------------------------------------------------------------------------
# Availability classification → LLMUnavailableError
# ---------------------------------------------------------------------------


def test_connection_error_raises_unavailable_after_retries():
    with patch("strivee_btwb.core.llm.time.sleep"):
        with patch(
            "strivee_btwb.core.llm.ollama.chat",
            side_effect=httpx.ConnectError("connection refused"),
        ) as mock_chat:
            with pytest.raises(LLMUnavailableError, match="unreachable"):
                chat_text("p", "qwen3:8b", retries=2)
    # initial attempt + 2 retries
    assert mock_chat.call_count == 3


def test_model_not_found_response_error_raises_unavailable():
    err = ollama.ResponseError("model 'qwen3:8b' not found, try pulling it first")
    with patch("strivee_btwb.core.llm.time.sleep"):
        with patch("strivee_btwb.core.llm.ollama.chat", side_effect=err):
            with pytest.raises(LLMUnavailableError):
                chat_text("p", "qwen3:8b")


def test_retry_then_success_returns_content():
    calls = {"n": 0}

    def flaky(**_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return _response("recovered")

    with patch("strivee_btwb.core.llm.time.sleep"):
        with patch("strivee_btwb.core.llm.ollama.chat", side_effect=flaky):
            assert chat_text("p", "m", retries=2) == "recovered"
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Content problems propagate as their own exception (NOT LLMUnavailableError)
# ---------------------------------------------------------------------------


def test_non_availability_response_error_propagates_unchanged():
    err = ollama.ResponseError("invalid role specified in the request")
    with patch("strivee_btwb.core.llm.ollama.chat", side_effect=err):
        with pytest.raises(ollama.ResponseError):
            chat_text("p", "m")


def test_generic_error_propagates_unchanged():
    with patch("strivee_btwb.core.llm.ollama.chat", side_effect=KeyError("message")):
        with pytest.raises(KeyError):
            chat_text("p", "m")
