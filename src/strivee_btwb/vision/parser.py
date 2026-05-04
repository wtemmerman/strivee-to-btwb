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

import ollama
from json_repair import repair_json

from ..core import config
from ..core.models import DayProgramming, ProgrammingBlock

logger = logging.getLogger("vision")

_TEXT_PROMPT_TEMPLATE = """Extract every CrossFit programming block from the Strivee accessibility text below for {day_label}.

OUTPUT FORMAT — follow exactly, no exceptions:
{{"blocks": [{{"name": "BLOCK_TITLE", "content": "PRESCRIPTION_HERE", "instruction": "COACHING_NOTES_HERE"}}]}}

Your response must be ONLY that JSON object — no explanation, no markdown, no code fences, no text before or after.
NEVER use "BLOCK_TITLE", "PRESCRIPTION_HERE", or "COACHING_NOTES_HERE" literally — those are placeholders.

━━━ IDENTIFYING BLOCKS ━━━
A Strivee block title ALWAYS matches one of these two patterns:
  • Starts with "EMF"  →  e.g. "EMF 60 : Snatch", "EMF RX - Optional RUN", "EMF Rx : Pull-up"
  • Starts with an emoji followed by a sport/category name  →  e.g. "🏊🏼‍♂️Swim Workout"

Everything between a block title and the next block title (or end of text) belongs to that block.

NOT block titles — these are sub-section headers WITHIN the current block, keep their text as part of the block content:
  • Lines ending with " -"  (e.g. "Warm-up -", "Main Part -", "Cooldown -", "Rest 3 min jogging between sets -")
  • Lines starting with "📌"  (e.g. "📌Échauffement", "📌Session")
  • Any other short label that does not match the EMF / emoji-category patterns above

━━━ CONTENT (prescription only) ━━━
"content" = the minimal statement of what to DO, copied exactly:
  • Conditioning (AMRAP/EMOM/For Time/Run/Swim sets): time domain + movements + distances + reps + weights
    — Include sub-section labels like "Warm-up -", "Main Part -", "Cooldown -" as structural markers in content
  • Strength (Build to / Find / Work to): ONLY the single goal sentence
    e.g. "Build to a 1RM Squat Snatch for the day"  ← that one line is the entire content
  • Do NOT include percentages-as-progressions, coaching explanations, or Objectif text in content

━━━ INSTRUCTION (everything else) ━━━
"instruction" = all coaching, guidance, and context — NOT the core prescription.
These markers ALWAYS begin an instruction section; move the marker line AND everything after it to instruction:
  • "Objectif"
  • "Gamme suggéré"
  • "Tentatives lourdes"
  • "Si vous ratez" / "Si vous manquez"
  • "Score :"
  • "Compare" / "RPE" / "Niveau" / "Effort"
  Any sentence that explains, motivates, or coaches rather than prescribes → instruction.
  When in doubt → instruction.  If no instruction exists → use ""

━━━ LEVEL LABELS ━━━
Level labels (RX 🔱, INTER+ 🪖, INTER 🎖️, etc.) are section headers — skip the label line itself, never include it in content or instruction.

━━━ RX FILTERING ━━━
When multiple levels exist, keep ONLY the RX 🔱 section.
Remove everything from the first INTER+ or INTER label onward (until the next block title).
Example:
  Input:  "AMRAP 12:00 / 24 DU / 6 PC #50kg / 6 HSPU  [then]  INTER+ 🪖 / AMRAP 12:00 / ..."
  Output content: "AMRAP 12:00\\n24 DU\\n6 PC #50kg\\n6 HSPU"

━━━ IGNORE COMPLETELY ━━━
- Day-tab labels: LUN, MAR, MER, JEU, VEN, SAM, DIM and Mon/Tue/Wed/Thu/Fri/Sat/Sun
- Lines that are a single number 1-31 (date numbers in the week strip)
- App header lines (e.g. "EMF 60'", "EMF 45'")
- Bottom nav tabs: WOD, Box, Noter, PRs, Profil
- Lines matching "N Scores", "N Score", "N Media", "N Media" where N is a number
- Lines starting with http:// or https://
- Announcements: WhatsApp groups, Zoom/Meet calls, weekly call banners
- SKIP any block whose name contains: {excluded}
- STOP at the first line containing "Inviter un ami" — ignore everything from that line onward

Text:
{text}"""


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
    response = ollama.chat(
        model=model,
        think=False,  # suppress qwen3 thinking tokens that produce empty visible output
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response["message"]["content"]
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
    logger.info("%s: %d block(s) extracted", day_label, len(blocks))
    return DayProgramming(date=target_date, day_label=day_label, blocks=blocks)
