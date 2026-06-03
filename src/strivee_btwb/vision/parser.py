"""
Text parsing: send accessibility-tree text to a local Ollama text model and
extract structured CrossFit programming data.

The model receives the raw text lines collected from Android's UI automator dump
for one day and returns a JSON object listing every programming block. Raw LLM
output is sanitised and repaired before parsing so that common formatting quirks
don't cause failures.
"""

import json
import logging
import re

from json_repair import repair_json

from ..core import config
from ..core.llm import chat_text
from ..core.models import DayProgramming, ProgrammingBlock
from ..prompts import load

logger = logging.getLogger("vision")

_TEXT_PROMPT_TEMPLATE = load("parse_day.txt")


def _sanitize_json_strings(s: str) -> str:
    """Replace unescaped control characters inside JSON string literals.

    LLMs often emit literal newlines in string values, which is invalid JSON
    but intended as \\n.
    """
    result = []
    in_string = False
    escaped = False
    _escapes = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    for ch in s:
        if escaped:
            result.append(ch)
            escaped = False
        elif ch == "\\" and in_string:
            result.append(ch)
            escaped = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif in_string and ch in _escapes:
            result.append(_escapes[ch])
        else:
            result.append(ch)
    return "".join(result)


def _extract_json(text: str) -> str:
    """Extract and repair the first JSON object or array from raw model output.

    Handles markdown code fences, leading prose, unescaped control characters,
    bare JSON arrays (model returned [{...}] instead of {"blocks": [...]}),
    and the LLM habit of prematurely closing the blocks array between items.
    Raises ValueError if no JSON value can be found.
    """
    stripped = text.strip()
    if "```" in stripped:
        stripped = stripped.split("```", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0]
        if stripped and stripped.lstrip()[0:1] not in ("{", "["):
            stripped = stripped.split("\n", 1)[-1]

    arr_start = stripped.find("[")
    obj_start = stripped.find("{")

    # Prefer whichever delimiter appears first; fall back to the other
    if arr_start != -1 and (obj_start == -1 or arr_start < obj_start):
        arr_end = stripped.rfind("]")
        if arr_end > arr_start:
            json_str = _sanitize_json_strings(stripped[arr_start : arr_end + 1])
            try:
                json.loads(json_str)
                return json_str
            except json.JSONDecodeError:
                return repair_json(json_str, ensure_ascii=False)

    start = obj_start
    end = stripped.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"No JSON value found in model response. First 400 chars:\n{text[:400]}")

    json_str = _sanitize_json_strings(stripped[start : end + 1])

    try:
        json.loads(json_str)
    except json.JSONDecodeError:
        # Fix LLM habit of closing the blocks array prematurely between items:
        # ], \n{"name": ...  →  , \n{"name": ...
        json_str = re.sub(r'\]\s*,\s*(\{"name")', r", \1", json_str)
        try:
            json.loads(json_str)
        except json.JSONDecodeError:
            json_str = repair_json(json_str, ensure_ascii=False)

    return json_str


def _is_excluded(name: str) -> bool:
    """Return True if the block name contains any excluded string (case-insensitive).

    Uses substring match so emoji-prefixed names like '🔥 Warm-up 🔥' are caught
    by the 'Warm-up' entry without needing to list every emoji variant.
    """
    name_lower = name.lower()
    return any(ex.lower() in name_lower for ex in config.EXCLUDED_BLOCKS)


# A real "EMF ..." block title starts with EMF, a level (number / RX), and has a
# name after a ':' or '-' separator. This deliberately excludes the app header
# lines "EMF 60'" / "EMF 45'" (a minute/prime symbol, no separator).
_EMF_TITLE_RE = re.compile(r"^EMF\s+(?:\d+|RX)\b.*[:\-]", re.IGNORECASE)


def count_block_titles(text: str) -> list[str]:
    """List the non-excluded ``EMF ...`` block titles found in *text*.

    This is a deterministic lower bound on how many blocks the model should
    return: it is instructed to emit one entry per title, so a shorter result
    means a block was dropped. Emoji-category titles are intentionally NOT
    counted — they are hard to tell apart from difficulty headers (🔱/🪖/🎖️)
    and sub-section markers (📌) without the model's judgement, and are almost
    always excluded blocks anyway. Using only EMF titles keeps this a
    false-positive-free signal.
    """
    return [
        s
        for line in text.splitlines()
        if (s := line.strip()) and _EMF_TITLE_RE.match(s) and not _is_excluded(s)
    ]


def extract_day_programming_from_text(
    text: str,
    day_label: str,
    target_date,
    model: str | None = None,
) -> DayProgramming:
    """Parse programming blocks from plain accessibility-tree text (no images).

    Args:
        text: Raw text lines collected from Android UI dump.
        day_label: Short weekday label (e.g. ``"Mon"``).
        target_date: Calendar date the day corresponds to.
        model: Ollama model tag; defaults to ``config.OLLAMA_TEXT_MODEL``.

    Returns:
        A :class:`~strivee_btwb.models.DayProgramming` with all non-excluded blocks.

    Raises:
        ValueError: If the model returns output that cannot be parsed as JSON.
        LLMUnavailableError: If Ollama is unreachable or the model is missing.
    """
    model = model or config.OLLAMA_TEXT_MODEL
    excluded_str = ", ".join(config.EXCLUDED_BLOCKS) if config.EXCLUDED_BLOCKS else "none"
    prompt = _TEXT_PROMPT_TEMPLATE.format(
        day_label=day_label,
        excluded=excluded_str,
        text=text,
    )

    logger.info("Parsing %s from text dump (%d chars) with %s", day_label, len(text), model)

    logger.debug("%s: prompt:\n%s", day_label, prompt)
    raw = chat_text(prompt, model)
    logger.debug("%s: raw text-parse response:\n%s", day_label, raw)

    if not raw.strip():
        logger.warning("%s: model returned empty response", day_label)
        data: dict = {}
    else:
        try:
            json_str = _extract_json(raw)
            data = json.loads(json_str)
        except (ValueError, json.JSONDecodeError) as e:
            raise ValueError(
                f"Text parsing failed for {day_label}: {e}\n\nModel response:\n{raw}"
            ) from e

    if isinstance(data, list):
        data = {"blocks": data}
    if not data.get("blocks"):
        list_vals = [v for v in data.values() if isinstance(v, list)]
        if list_vals:
            data = {"blocks": list_vals[0]}
        elif data and all(isinstance(v, str) for v in data.values()):
            data = {"blocks": [{"name": k, "content": v} for k, v in data.items()]}

    all_blocks = data.get("blocks", [])
    blocks = [
        ProgrammingBlock(name=b["name"], content=b["content"], instruction=b.get("instruction", ""))
        for b in all_blocks
        if not _is_excluded(b.get("name", ""))
    ]
    excluded_count = len(all_blocks) - len(blocks)
    if excluded_count:
        logger.debug("%s: %d block(s) dropped by exclusion filter", day_label, excluded_count)

    expected_titles = count_block_titles(text)
    if len(blocks) < len(expected_titles):
        logger.warning(
            "%s: extracted %d block(s) but the source has %d EMF block title(s) — "
            "the model may have dropped a block. Source titles: %s",
            day_label,
            len(blocks),
            len(expected_titles),
            expected_titles,
        )

    logger.info("%s: %d block(s) extracted", day_label, len(blocks))
    return DayProgramming(date=target_date, day_label=day_label, blocks=blocks)
