"""Centralised access to the local Ollama model.

Every Ollama call in the pipeline goes through this module so that retry
behaviour and error classification live in one place.

The key distinction this module enforces:

* **Infrastructure failure** — Ollama is not running, the model is not pulled,
  or the connection drops. No usable response can be produced, so we raise
  :class:`LLMUnavailableError` and the caller should abort the whole run rather
  than silently degrade.
* **Content problem** — the service answered but the answer is empty or badly
  shaped. That is returned to the caller, which decides whether to repair it,
  retry with a fallback model, or keep the original input.

Conflating the two is dangerous for this tool: a silent fallback on an
infrastructure failure means raw, unformatted programming gets posted to BTWB.
"""

import logging
import time

import httpx
import ollama

logger = logging.getLogger("llm")


class LLMUnavailableError(RuntimeError):
    """Raised when Ollama cannot be reached or the requested model is missing.

    Distinct from a model returning poor content: this means no usable response
    was produced at all, so callers should stop rather than fall back.
    """


# Exception types that always mean "the service/model is unavailable", never a
# content problem.
_UNAVAILABLE_EXC: tuple[type[Exception], ...] = (
    ConnectionError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    ollama.RequestError,
)

# Substrings that mark an ``ollama.ResponseError`` as an availability problem
# (e.g. "model 'qwen3:8b' not found", "connection refused").
_UNAVAILABLE_MARKERS: tuple[str, ...] = (
    "connection",
    "connect",
    "refused",
    "not found",
    "no such host",
    "timeout",
    "unavailable",
)


def _is_unavailable(exc: Exception) -> bool:
    """Classify *exc* as an availability failure (True) or a content problem (False)."""
    if isinstance(exc, _UNAVAILABLE_EXC):
        return True
    if isinstance(exc, ollama.ResponseError):
        msg = str(exc).lower()
        return any(marker in msg for marker in _UNAVAILABLE_MARKERS)
    return False


def _chat(
    prompt: str,
    model: str,
    *,
    fmt: dict | str | None = None,
    retries: int = 2,
    backoff_s: float = 1.0,
) -> str:
    """Send *prompt* to *model* and return the raw message content.

    Retries transient availability failures up to *retries* times before raising
    :class:`LLMUnavailableError`. Non-availability errors propagate immediately.
    """
    kwargs: dict = {
        "model": model,
        "think": False,  # suppress qwen3 thinking tokens that produce empty visible output
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": 0},
    }
    if fmt is not None:
        kwargs["format"] = fmt

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = ollama.chat(**kwargs)
            return response["message"]["content"]
        except Exception as exc:
            # Re-classified below: availability failures retry/raise LLMUnavailableError,
            # everything else propagates unchanged.
            last_exc = exc
            if not _is_unavailable(exc):
                raise
            if attempt < retries:
                logger.warning(
                    "Ollama unavailable (%s) — retry %d/%d in %.0fs",
                    exc,
                    attempt + 1,
                    retries,
                    backoff_s,
                )
                time.sleep(backoff_s)
                continue
            break

    raise LLMUnavailableError(
        f"Ollama is unreachable or model '{model}' is unavailable ({last_exc}). "
        f"Start Ollama ('ollama serve') and pull the model ('ollama pull {model}')."
    ) from last_exc


def chat_text(prompt: str, model: str, *, retries: int = 2) -> str:
    """Return the model's raw text response for *prompt*.

    Raises :class:`LLMUnavailableError` if Ollama/the model cannot be reached.
    """
    return _chat(prompt, model, retries=retries)


def chat_json(
    prompt: str,
    model: str,
    *,
    schema: dict | None = None,
    retries: int = 2,
) -> str:
    """Return the model's response constrained to JSON.

    When *schema* is provided it is passed to Ollama's structured-output
    ``format`` parameter so the model is forced to emit valid JSON matching the
    schema. With no schema, plain ``"json"`` mode is requested.

    Raises :class:`LLMUnavailableError` if Ollama/the model cannot be reached.
    """
    return _chat(prompt, model, fmt=schema if schema is not None else "json", retries=retries)
