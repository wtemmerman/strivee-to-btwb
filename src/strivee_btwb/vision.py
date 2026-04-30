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

from . import config
from .models import DayProgramming, ProgrammingBlock

logger = logging.getLogger("vision")

_DAY_PROMPT_TEMPLATE = """You are a CrossFit programming parser. The {count} image(s) show the {day_label} programming from the Strivee app — each image is one scroll position, from top to bottom.

Extract every programming block visible across all images. Blocks have names like "Back Squat", "WOD", "Accessory", etc.

Return ONLY a raw JSON object — no markdown, no code fences, no text before or after the JSON:
{{
  "blocks": [
    {{"name": "Back Squat", "content": "5x5 @ 80%"}},
    {{"name": "WOD", "content": "..."}}
  ]
}}

Rules:
- Copy the exact text visible in the images — do not invent or paraphrase
- Preserve rep schemes, percentages, weights, timing, line breaks exactly as shown
- Each block name appears only once — merge content if a block continues across scroll positions
- Ignore UI chrome (status bar, navigation bar, video thumbnails)
- SKIP any block whose name starts with: {excluded}"""


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
    """Extract and repair the first JSON object from raw model output.

    Handles markdown code fences, leading prose, unescaped control characters,
    and the LLM habit of prematurely closing the blocks array between items.
    Raises ValueError if no JSON object can be found.
    """
    stripped = text.strip()
    if "```" in stripped:
        stripped = stripped.split("```", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0]
        if stripped and not stripped.lstrip().startswith("{"):
            stripped = stripped.split("\n", 1)[-1]

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"No JSON object found in model response. First 400 chars:\n{text[:400]}")

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
        messages=[
            {
                "role": "user",
                "content": prompt,
                "images": [_to_bytes(img) for img in images],
            }
        ],
    )

    raw = response["message"]["content"]
    try:
        json_str = _extract_json(raw)
        data = json.loads(json_str)
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(
            f"Vision parsing failed for {day_label}: {e}\n\nModel response:\n{raw}"
        ) from e

    blocks = [
        ProgrammingBlock(name=b["name"], content=b["content"])
        for b in data.get("blocks", [])
        if not _is_excluded(b.get("name", ""))
    ]
    logger.info("%s: %d block(s) extracted", day_label, len(blocks))
    return DayProgramming(date=target_date, day_label=day_label, blocks=blocks)
