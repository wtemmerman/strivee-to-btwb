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
from ..core.llm import chat_json
from ..core.models import DayProgramming, ProgrammingBlock
from ..prompts import load

logger = logging.getLogger("vision")

_TEXT_PROMPT_TEMPLATE = load("parse_day.txt")
_RECOVER_PROMPT_TEMPLATE = load("recover_block.txt")

# Single-block recovery output: just the prescription + coaching notes for one
# named block (the title is already known from the regex that found it).
_RECOVER_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string"},
        "instruction": {"type": "string"},
    },
    "required": ["content"],
}

# JSON schema passed to Ollama's structured-output `format`. The model is
# grammar-constrained to emit valid JSON in this shape, which removes almost all
# of the historical JSON-repair fragility at the source. _extract_json is kept
# as a fallback for Ollama/model setups that do not honour the schema.
_BLOCKS_SCHEMA = {
    "type": "object",
    "properties": {
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                    "instruction": {"type": "string"},
                },
                "required": ["name", "content"],
            },
        }
    },
    "required": ["blocks"],
}


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


def _normalise_shape(data: object) -> dict:
    """Coerce assorted model response shapes into a ``{"blocks": [...]}`` dict.

    The schema-constrained path returns this shape directly; this only matters
    for the fallback path, where a model may emit a bare list, a single block
    object, a ``{"blocks": ...}`` synonym keyed differently, or a flat
    ``{name: content}`` mapping.
    """
    if isinstance(data, list):
        return {"blocks": data}
    if not isinstance(data, dict):
        return {"blocks": []}

    blocks: list = []
    if isinstance(data.get("blocks"), list):
        blocks = data["blocks"]
    elif "name" in data:
        # A single unwrapped block object — wrap it; do NOT explode its fields
        # into bogus blocks named "name"/"content".
        blocks = [data]
    elif list_vals := [v for v in data.values() if isinstance(v, list)]:
        blocks = list_vals[0]
    elif data and all(isinstance(v, str) for v in data.values()):
        blocks = [{"name": k, "content": v} for k, v in data.items()]
    return {"blocks": blocks}


def _validate_blocks(raw_blocks: object, day_label: str) -> list[dict]:
    """Return well-formed ``{name, content, instruction}`` dicts from the model output.

    Drops entries that are not objects or have no usable name, logging each, so a
    single malformed entry can never abort the whole day.
    """
    if not isinstance(raw_blocks, list):
        return []
    valid: list[dict] = []
    for b in raw_blocks:
        if not isinstance(b, dict):
            logger.warning("%s: dropping non-object block entry: %r", day_label, b)
            continue
        name = str(b.get("name", "")).strip()
        if not name:
            logger.warning("%s: dropping block with empty name: %r", day_label, b)
            continue
        valid.append(
            {
                "name": name,
                "content": str(b.get("content", "")),
                "instruction": str(b.get("instruction", "")),
            }
        )
    return valid


def _parse_blocks_response(raw: str, day_label: str) -> dict:
    """Parse the model's response into a ``{"blocks": [...]}`` dict.

    Fast path: structured output is valid JSON, so ``json.loads`` succeeds
    directly. Fallback: the tolerant :func:`_extract_json` repair path handles
    fences, stray prose, and unescaped control characters for setups where the
    schema is not honoured.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = json.loads(_extract_json(raw))
        except (ValueError, json.JSONDecodeError) as e:
            raise ValueError(
                f"Text parsing failed for {day_label}: {e}\n\nModel response:\n{raw}"
            ) from e
    return _normalise_shape(data)


# A real "EMF ..." block title starts with EMF, a level (a number or RX), then a
# ':'/'-' separator DIRECTLY after the level. Anchoring the separator right after
# the level (rather than anywhere in the line) avoids matching content lines that
# merely start with "EMF <n>" and contain a dash later, and excludes the app
# header lines "EMF 60'" / "EMF 45'" (prime symbol, no separator).
_EMF_TITLE_RE = re.compile(r"^EMF\s+(?:\d+|RX)\b\s*[:\-]", re.IGNORECASE)


def count_block_titles(text: str) -> list[str]:
    """List the non-excluded ``EMF ...`` block titles found in *text*.

    A conservative, best-effort lower bound on how many blocks the model should
    return: it is instructed to emit one entry per title, so a shorter result
    likely means a block was dropped. Emoji-category titles are intentionally NOT
    counted — they are hard to tell apart from difficulty headers (🔱/🪖/🎖️)
    and sub-section markers (📌) without the model's judgement, and are almost
    always excluded blocks anyway. The count only drives a warning and a
    fallback retry, never a hard failure, so an occasional miscount is harmless.
    """
    return [
        s
        for line in text.splitlines()
        if (s := line.strip()) and _EMF_TITLE_RE.match(s) and not _is_excluded(s)
    ]


def _norm_title(name: str) -> str:
    """Normalise a block title for set-membership comparison (case/space-insensitive)."""
    return " ".join(name.split()).casefold()


# ── Deterministic output cleanup ────────────────────────────────────────────
# The model is told to drop UI chrome (and we want emoji gone for BTWB), but it
# does neither reliably. Enforce both deterministically on the final
# content/instruction of every block. Safe by construction: the emoji ranges
# match only pictographs/dingbats and the chrome regex matches only whole Strivee
# UI lines — never workout prescription text.

_EMOJI_RE = re.compile(
    "["
    "\U0001f000-\U0001faff"  # emoji and pictographs (legs, trident, target, pin, fire)
    "\U00002600-\U000027bf"  # misc symbols and dingbats (arrows, check, star)
    "\U00002b00-\U00002bff"  # misc symbols and arrows
    "\ufe0f\u200d\u2640\u2642\u2122\u2139"  # VS16, ZWJ, gender signs, TM, info
    "]",
    flags=re.UNICODE,
)

# A whole line that is pure Strivee chrome: nav tabs, score/media counters, the
# "rejoindre Strivee" footer. Anchored to the full line so it never clips content.
_UI_CHROME_RE = re.compile(
    r"^(?:wod|box|noter|prs|profil|\d+\s*scores?|\d+\s*medias?|"
    r"à rejoindre strivee|tous les outils.*)$",
    re.IGNORECASE,
)

_INVITE_FOOTER_RE = re.compile(r"\s*inviter un ami", re.IGNORECASE)

# Lines that begin TRAILING coaching/guidance. In Strivee blocks coaching always
# follows the prescription, so when one of these appears in content, it and every
# line after it is relocated to instruction. Kept deliberately narrow — only
# markers that never start a prescription line (NOT e.g. "Max rep ...") — so a
# real movement line is never moved.
_TRAILING_COACH_RE = re.compile(
    r"^\s*-?\s*(?:objectif|gammes?\b|gamme sugg|notes?\b|notez|mat[ée]riel|"
    r"quel niveau|si vous (?:ratez|manquez)|tentatives lourdes|conseil)\b",
    re.IGNORECASE,
)


def _resplit_trailing_coaching(content: str, instruction: str) -> tuple[str, str]:
    """Move trailing coaching that leaked into content over to instruction.

    The model sometimes leaves "Notes :", "Gammes :", "Matériel :", "Objectif",
    "Quel niveau" etc. in content. These always trail the prescription, so the
    first such marker line and everything after it is relocated to the front of
    instruction. No-op until at least one prescription line has been seen, so a
    block the model mis-split into all-coaching is left for its own judgement.
    """
    lines = content.splitlines()
    seen_prescription = False
    for i, line in enumerate(lines):
        if _TRAILING_COACH_RE.match(line):
            if seen_prescription:
                kept = "\n".join(lines[:i]).strip()
                moved = "\n".join(lines[i:]).strip()
                merged = f"{moved}\n\n{instruction}".strip() if instruction.strip() else moved
                return kept, merged
            continue  # leading coaching marker: not a prescription, don't arm the split
        if line.strip():
            seen_prescription = True
    return content, instruction


# A placeholder movement slot ("X Gymnastics Movement") and the inline RX value
# that fills it ("RX - 5 Ring Muscle-up"). When both are present 1:1 in content,
# substitute the value into the slot and drop the now-redundant "RX - ..." line.
_PLACEHOLDER_RE = re.compile(r"^\s*X\b.*\bMovement\b\s*$", re.IGNORECASE)
_RX_VALUE_RE = re.compile(r"^\s*RX\s*[-:]\s*(.+?)\s*$", re.IGNORECASE)


def _fill_placeholder_movements(content: str) -> str:
    """Replace "X ... Movement" slots with their inline RX value, 1:1 only.

    No-op unless the count of placeholders equals the count of "RX - <value>"
    lines, so an ambiguous layout is never guessed at.
    """
    lines = content.splitlines()
    ph = [i for i, ln in enumerate(lines) if _PLACEHOLDER_RE.match(ln)]
    rx = [(i, m.group(1)) for i, ln in enumerate(lines) if (m := _RX_VALUE_RE.match(ln))]
    if not ph or len(ph) != len(rx):
        return content
    values = [v for _, v in rx]
    rx_idx = {i for i, _ in rx}
    out: list[str] = []
    vi = 0
    for i, ln in enumerate(lines):
        if i in set(ph):
            out.append(values[vi])
            vi += 1
        elif i in rx_idx:
            continue
        else:
            out.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _clean_block_text(text: str) -> str:
    """Strip emoji and Strivee UI chrome from one content/instruction field.

    Drops everything from an "Inviter un ami" line onward (the invite footer ends
    the real text), removes whole-line nav/score chrome, strips emoji, and tidies
    the whitespace the removals leave behind while preserving paragraph breaks.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        if _INVITE_FOOTER_RE.match(raw):
            break
        if raw.strip() == "":
            lines.append("")  # keep intentional paragraph breaks
            continue
        line = _EMOJI_RE.sub("", raw)
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        if not line or _UI_CHROME_RE.match(line):
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _recover_block(text: str, title: str, model: str) -> ProgrammingBlock | None:
    """Re-extract a single named block the full-text parse merged or dropped.

    The model often fails the boundary between an excluded block and a small real
    block that follows it (it absorbs the real block's lines into the excluded
    one). count_block_titles still finds the title via regex, and asking the model
    for just that one block is a far easier task, so this recovers it reliably.

    Returns None (no block added) when the model yields nothing usable. Raises
    LLMUnavailableError on infrastructure failure, like every other model call.
    """
    prompt = _RECOVER_PROMPT_TEMPLATE.format(title=title, text=text)
    raw = chat_json(prompt, model, schema=_RECOVER_SCHEMA)
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = json.loads(_extract_json(raw))
        except (ValueError, json.JSONDecodeError):
            return None
    if not isinstance(data, dict):
        return None
    content = str(data.get("content", "")).strip()
    if not content:
        return None
    return ProgrammingBlock(
        name=title, content=content, instruction=str(data.get("instruction", "")).strip()
    )


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
    raw = chat_json(prompt, model, schema=_BLOCKS_SCHEMA)
    logger.debug("%s: raw text-parse response:\n%s", day_label, raw)

    if not raw.strip():
        logger.warning("%s: model returned empty response", day_label)
        data: dict = {"blocks": []}
    else:
        data = _parse_blocks_response(raw, day_label)

    all_blocks = _validate_blocks(data.get("blocks", []), day_label)
    blocks = [
        ProgrammingBlock(name=b["name"], content=b["content"], instruction=b["instruction"])
        for b in all_blocks
        if not _is_excluded(b["name"])
    ]
    excluded_count = len(all_blocks) - len(blocks)
    if excluded_count:
        logger.debug("%s: %d block(s) dropped by exclusion filter", day_label, excluded_count)

    expected_titles = count_block_titles(text)

    # Recover any non-excluded EMF title the model merged into a neighbour or
    # dropped: the regex above finds the title reliably, and a focused single-block
    # re-extraction succeeds where the full multi-block parse failed the boundary.
    present = {_norm_title(b.name) for b in blocks}
    for title in expected_titles:
        if _norm_title(title) in present:
            continue
        recovered = _recover_block(text, title, model)
        if recovered is not None:
            logger.info("%s: recovered dropped block '%s'", day_label, title)
            blocks.append(recovered)
            present.add(_norm_title(title))

    # Deterministic final cleanup (the model is unreliable at all of this). Order
    # matters: strip emoji + Strivee UI chrome FIRST so the structural passes see
    # clean text (e.g. "🔱 RX - ..." must become "RX - ..." before the placeholder
    # fill can match it), then relocate trailing coaching, then fill "X ... Movement"
    # slots. Block names are already clean EMF/emoji-category titles — leave them.
    cleaned: list[ProgrammingBlock] = []
    for b in blocks:
        content = _clean_block_text(b.content)
        instruction = _clean_block_text(b.instruction)
        content, instruction = _resplit_trailing_coaching(content, instruction)
        content = _fill_placeholder_movements(content)
        cleaned.append(b.replace(content=content, instruction=instruction))
    blocks = cleaned

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
