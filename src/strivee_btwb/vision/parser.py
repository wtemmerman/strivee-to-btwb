"""
Vision parsing: send screenshots to an Ollama vision model and extract
structured CrossFit programming data.

The model receives one or more scroll-position images per day and returns a
JSON object listing every programming block. Raw LLM output is sanitised and
repaired before parsing so that common formatting quirks don't cause failures.
"""

import io
import json
import logging
import re

import ollama
from json_repair import repair_json
from PIL import Image

from ..core import config
from ..core.models import DayProgramming, ProgrammingBlock

logger = logging.getLogger("vision")

_DAY_PROMPT_TEMPLATE = """OUTPUT FORMAT — follow exactly, no exceptions:
{{"blocks": [{{"name": "Back Squat", "content": "5x5 @ 80%"}}, {{"name": "WOD", "content": "..."}}]}}

Your response must be ONLY that JSON object. No explanation, no markdown, no code fences, no text before or after.

Task: extract every CrossFit programming block from the {count} Strivee screenshot(s) for {day_label} (each image is one scroll position, top to bottom).

Rules:
- Copy the exact text visible — do not invent or paraphrase
- Preserve rep schemes, percentages, weights, timing, and line breaks exactly as shown
- Each block name appears once — merge content if a block continues across scroll positions
- Ignore UI chrome (status bar, nav bar, video thumbnails)
- SKIP any block whose name starts with: {excluded}"""

_REFORMAT_PROMPT_TEMPLATE = """Convert the CrossFit programming text below into this exact JSON format.
Your response must be ONLY the JSON — no explanation, no markdown, no code fences.

{{"blocks": [{{"name": "block name", "content": "full block text"}}]}}

Text to convert:
{text}"""


def _to_bytes(image: Image.Image) -> bytes:
    """Encode a PIL image to PNG bytes for Ollama's image field."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


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
        if stripped and not stripped.lstrip()[0:1] in ("{", "["):
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
    """Return True if the block name starts with any excluded prefix (case-insensitive)."""
    name_lower = name.lower()
    return any(name_lower.startswith(ex.lower()) for ex in config.EXCLUDED_BLOCKS)


def extract_day_programming(
    images: list[Image.Image],
    day_label: str,
    target_date,
    model: str | None = None,
) -> DayProgramming:
    """Parse all programming blocks for a single day from one or more screenshots.

    All scroll-position frames are sent together in one Ollama message so the
    model sees every frame without repeating UI chrome between calls. Blocks
    whose names match EXCLUDED_BLOCKS (prefix, case-insensitive) are dropped.

    Args:
        images: Ordered list of cropped screenshots (top → bottom scroll positions).
        day_label: Short weekday label shown in Strivee (e.g. ``"Mon"``).
        target_date: Calendar date the day corresponds to.
        model: Ollama model tag; defaults to ``config.OLLAMA_MODEL``.

    Returns:
        A :class:`~strivee_btwb.models.DayProgramming` with all non-excluded blocks.

    Raises:
        ValueError: If the model returns output that cannot be parsed as JSON.
    """
    model = model or config.OLLAMA_MODEL
    excluded_str = ", ".join(config.EXCLUDED_BLOCKS) if config.EXCLUDED_BLOCKS else "none"
    prompt = _DAY_PROMPT_TEMPLATE.format(
        day_label=day_label,
        count=len(images),
        excluded=excluded_str,
    )

    logger.info("Parsing %s (%d image(s)) with %s", day_label, len(images), model)
    response = ollama.chat(
        model=model,
        think=False,  # suppress qwen3 thinking tokens that produce empty visible output
        messages=[
            {
                "role": "user",
                "content": prompt,
                "images": [_to_bytes(img) for img in images],
            }
        ],
    )

    raw = response["message"]["content"]
    logger.debug("%s: raw vision response:\n%s", day_label, raw)
    data = None
    if not raw.strip():
        logger.warning("%s: vision model returned empty response — skipping reformat", day_label)
    else:
        try:
            json_str = _extract_json(raw)
            data = json.loads(json_str)
        except (ValueError, json.JSONDecodeError):
            # Model returned plain text instead of JSON (common with thinking models).
            # Send the extracted text back without images for a fast reformat pass.
            logger.warning("%s: no JSON in vision response — retrying as text reformat", day_label)
            reformat_prompt = _REFORMAT_PROMPT_TEMPLATE.format(text=raw)
            reformat_response = ollama.chat(
                model=model,
                think=False,
                messages=[{"role": "user", "content": reformat_prompt}],
            )
            raw2 = reformat_response["message"]["content"]
            logger.debug("%s: reformat response:\n%s", day_label, raw2)
            try:
                json_str = _extract_json(raw2)
                data = json.loads(json_str)
            except (ValueError, json.JSONDecodeError) as e:
                raise ValueError(
                    f"Vision parsing failed for {day_label}: {e}\n\nModel response:\n{raw}"
                ) from e

    if data is None:
        data = {}
    if isinstance(data, list):
        data = {"blocks": data}
    # Normalise wrong top-level key
    if not data.get("blocks"):
        list_vals = [v for v in data.values() if isinstance(v, list)]
        if list_vals:
            # e.g. {"converted_data": [{...}]} — use first list
            data = {"blocks": list_vals[0]}
        elif data and all(isinstance(v, str) for v in data.values()):
            # e.g. {"Block Name": "content"} — model used name-as-key format
            data = {"blocks": [{"name": k, "content": v} for k, v in data.items()]}
    all_blocks = data.get("blocks", [])
    blocks = [
        ProgrammingBlock(name=b["name"], content=b["content"])
        for b in all_blocks
        if not _is_excluded(b.get("name", ""))
    ]
    excluded_count = len(all_blocks) - len(blocks)
    if excluded_count:
        logger.debug("%s: %d block(s) dropped by exclusion filter", day_label, excluded_count)
    logger.info("%s: %d block(s) extracted", day_label, len(blocks))
    return DayProgramming(date=target_date, day_label=day_label, blocks=blocks)
