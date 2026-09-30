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


def test_chat_sets_num_ctx_and_keep_alive():
    # num_ctx guards against silent truncation of the large parse prompt; keep_alive
    # keeps the model resident across analyse/format calls of a run.
    import strivee_btwb.core.config as cfg

    with patch("strivee_btwb.core.llm.ollama.chat", return_value=_response("x")) as mock_chat:
        chat_text("p", "m")
        kwargs = mock_chat.call_args.kwargs
        assert kwargs["options"]["num_ctx"] == cfg.OLLAMA_NUM_CTX
        assert kwargs["options"]["temperature"] == 0
        assert kwargs["keep_alive"] == cfg.OLLAMA_KEEP_ALIVE


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
            with pytest.raises(LLMUnavailableError, match="Ollama call failed"):
                chat_text("p", "qwen3:8b", retries=2)
    # initial attempt + 2 retries (transient connection error is retried)
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
# Any failed call → LLMUnavailableError (never silently degraded). A successful
# call never raises, so a non-transient exception means no usable answer.
# ---------------------------------------------------------------------------


def test_response_error_becomes_unavailable_without_retry():
    # A server-side error (HTTP 5xx, model not pulled, bad request) is not a
    # transient connection error: fail loud immediately, do not retry.
    err = ollama.ResponseError("internal server error")
    with patch("strivee_btwb.core.llm.time.sleep") as mock_sleep:
        with patch("strivee_btwb.core.llm.ollama.chat", side_effect=err) as mock_chat:
            with pytest.raises(LLMUnavailableError):
                chat_text("p", "m")
    assert mock_chat.call_count == 1  # no retry for non-transient failures
    mock_sleep.assert_not_called()


def test_generic_error_becomes_unavailable():
    with patch("strivee_btwb.core.llm.ollama.chat", side_effect=KeyError("message")):
        with pytest.raises(LLMUnavailableError):
            chat_text("p", "m")


def test_none_content_returns_empty_string():
    # A thinking-only / empty assistant turn yields content=None; normalise to "".
    with patch("strivee_btwb.core.llm.ollama.chat", return_value={"message": {"content": None}}):
        assert chat_text("p", "m") == ""


# ---------------------------------------------------------------------------
# Response cache (benchmark only)
# ---------------------------------------------------------------------------


@pytest.fixture
def cached(tmp_path, monkeypatch):
    """Cache on, a pulled model with a known digest, and the model call recorded."""
    from strivee_btwb.core import llm

    llm._model_digest.cache_clear()
    digest = {"value": "sha256:aaa"}
    monkeypatch.setattr(
        "strivee_btwb.core.llm.ollama.list",
        lambda: {"models": [{"model": "qwen3:8b", "digest": digest["value"]}]},
    )
    llm.use_response_cache(tmp_path)
    yield digest
    llm.use_response_cache(None)
    llm._model_digest.cache_clear()


def test_a_repeated_call_is_answered_from_the_cache(cached):
    from strivee_btwb.core.llm import response_cache_stats

    with patch("strivee_btwb.core.llm.ollama.chat", return_value=_response("A")) as mock_chat:
        assert chat_text("prompt", "qwen3:8b") == "A"
        assert chat_text("prompt", "qwen3:8b") == "A"
    assert mock_chat.call_count == 1
    assert response_cache_stats() == (1, 1)


def test_a_changed_prompt_or_schema_asks_the_model(cached):
    with patch("strivee_btwb.core.llm.ollama.chat", return_value=_response("{}")) as mock_chat:
        chat_json("prompt", "qwen3:8b")
        chat_json("prompt edited", "qwen3:8b")
        chat_json("prompt", "qwen3:8b", schema={"type": "object"})
    assert mock_chat.call_count == 3


def test_an_updated_model_asks_again(cached):
    """Re-pulling the model changes its digest — old answers no longer apply."""
    from strivee_btwb.core import llm

    with patch("strivee_btwb.core.llm.ollama.chat", return_value=_response("A")) as mock_chat:
        chat_text("prompt", "qwen3:8b")
        cached["value"] = "sha256:bbb"
        llm._model_digest.cache_clear()
        chat_text("prompt", "qwen3:8b")
    assert mock_chat.call_count == 2


def test_an_empty_answer_is_cached_too(cached):
    """The pipeline handles "" deterministically, so it is as reusable as any answer."""
    with patch("strivee_btwb.core.llm.ollama.chat", return_value=_response(None)) as mock_chat:
        assert chat_text("prompt", "qwen3:8b") == ""
        assert chat_text("prompt", "qwen3:8b") == ""
    assert mock_chat.call_count == 1


def test_a_failed_call_is_not_cached(cached):
    with patch("strivee_btwb.core.llm.ollama.chat", side_effect=RuntimeError("500")):
        with pytest.raises(LLMUnavailableError):
            chat_text("prompt", "qwen3:8b")
    with patch("strivee_btwb.core.llm.ollama.chat", return_value=_response("A")) as mock_chat:
        assert chat_text("prompt", "qwen3:8b") == "A"
    assert mock_chat.call_count == 1


def test_a_model_that_is_not_pulled_fails_loud(cached):
    with pytest.raises(LLMUnavailableError, match="not pulled"):
        chat_text("prompt", "llama3:70b")


def test_without_the_cache_every_call_asks_the_model():
    with patch("strivee_btwb.core.llm.ollama.chat", return_value=_response("A")) as mock_chat:
        chat_text("prompt", "qwen3:8b")
        chat_text("prompt", "qwen3:8b")
    assert mock_chat.call_count == 2


def test_an_online_run_records_the_digest_for_offline_replay(cached, tmp_path):
    import json

    with patch("strivee_btwb.core.llm.ollama.chat", return_value=_response("A")):
        chat_text("prompt", "qwen3:8b")
    assert json.loads((tmp_path / "models.json").read_text()) == {"qwen3:8b": "sha256:aaa"}


def test_offline_replay_answers_from_the_cache_without_ollama(cached, tmp_path, monkeypatch):
    from strivee_btwb.core import llm

    with patch("strivee_btwb.core.llm.ollama.chat", return_value=_response("A")):
        chat_text("prompt", "qwen3:8b")

    def no_ollama(*_a, **_k):
        raise AssertionError("offline replay must not reach Ollama")

    monkeypatch.setattr("strivee_btwb.core.llm.ollama.list", no_ollama)
    monkeypatch.setattr("strivee_btwb.core.llm.ollama.chat", no_ollama)
    llm.use_response_cache(tmp_path, offline=True)
    assert chat_text("prompt", "qwen3:8b") == "A"
    with pytest.raises(LLMUnavailableError, match="no cached answer"):
        chat_text("a prompt the code never asked before", "qwen3:8b")


def test_offline_replay_without_a_recorded_digest_fails_loud(tmp_path):
    from strivee_btwb.core import llm

    llm.use_response_cache(tmp_path, offline=True)
    try:
        with pytest.raises(LLMUnavailableError, match="no digest recorded"):
            chat_text("prompt", "qwen3:8b")
    finally:
        llm.use_response_cache(None)
