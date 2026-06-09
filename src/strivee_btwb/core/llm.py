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

from . import config

logger = logging.getLogger("llm")


class LLMUnavailableError(RuntimeError):
    """Raised when Ollama cannot be reached or the requested model is missing.

    Distinct from a model returning poor content: this means no usable response
    was produced at all, so callers should stop rather than fall back.
    """


# Transient connection/transport failures worth retrying. These are matched by
# TYPE (not by message text), so classification never drifts with Ollama/httpx
# wording. A successful ollama.chat() call never raises — any exception means we
# got no usable answer, so every failure ultimately becomes LLMUnavailableError.
_TRANSIENT_EXC: tuple[type[Exception], ...] = (
    ConnectionError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
)


def _chat(
    prompt: str,
    model: str,
    *,
    fmt: dict | str | None = None,
    retries: int = 2,
    backoff_s: float = 1.0,
) -> str:
    """Send *prompt* to *model* and return the message content (``""`` if empty).

    Either returns a string or raises :class:`LLMUnavailableError`. Transient
    connection failures are retried up to *retries* times; any other failure of
    the call (server error, model not pulled, malformed response) is wrapped in
    :class:`LLMUnavailableError` without retry — we never guess infra-vs-content
    from the message text, and we never silently return a degraded result.
    """
    kwargs: dict = {
        "model": model,
        "think": False,  # suppress qwen3 thinking tokens that produce empty visible output
        "messages": [{"role": "user", "content": prompt}],
        # num_ctx must hold the full prompt + input: qwen3 pins none, so Ollama's
        # small default would silently truncate the start of the parse prompt.
        "options": {"temperature": 0, "num_ctx": config.OLLAMA_NUM_CTX},
        # Keep the model resident across the analyse/format calls of one run.
        "keep_alive": config.OLLAMA_KEEP_ALIVE,
    }
    if fmt is not None:
        kwargs["format"] = fmt

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = ollama.chat(**kwargs)
            content = response["message"]["content"]
            # Ollama types content as Optional[str]; a thinking-only/empty turn
            # yields None. Normalise to "" so callers can treat it as empty.
            return content if content is not None else ""
        except _TRANSIENT_EXC as exc:
            last_exc = exc
            if attempt < retries:
                logger.warning(
                    "Ollama unreachable (%s) — retry %d/%d in %.0fs",
                    exc,
                    attempt + 1,
                    retries,
                    backoff_s,
                )
                time.sleep(backoff_s)
                continue
            break
        except Exception as exc:
            # Non-transient failure of the call (HTTP 5xx, model not pulled, bad
            # response shape). Retrying won't help; fail loud so callers abort
            # rather than fall back to unformatted content.
            last_exc = exc
            break

    raise LLMUnavailableError(
        f"Ollama call failed for model '{model}' ({last_exc}). "
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
