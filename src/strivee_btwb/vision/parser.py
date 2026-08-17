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
from ..core.models import INTER, INTER_PLUS, RX, DayProgramming, ProgrammingBlock
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
#
# Deliberately no inter_plus / inter: asking the model to sort difficulty levels
# produced steady mislabels (it reads the emoji, which means INTER+ on one day and
# INTER on the next, and assumes RX comes first) and cost block-boundary accuracy
# elsewhere in the same response. The model copies each level verbatim into content
# and _extract_levels does the split from the header text.
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


def _source_slices(text: str) -> dict[str, str]:
    """Map each EMF title to the raw source lines beneath it, up to the next title.

    The title regex is reliable where the model is not, so these slices are the
    ground truth for what a block may contain.
    """
    lines = text.splitlines()
    starts = [
        (i, s) for i, line in enumerate(lines) if (s := line.strip()) and _EMF_TITLE_RE.match(s)
    ]
    slices: dict[str, str] = {}
    for n, (i, title) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        slices[_norm_title(title)] = "\n".join(lines[i + 1 : end])
    return slices


# Shorter lines ("+", "5x5", "Skill") recur all over a day and prove nothing about
# which block a prescription came from.
_DISTINCTIVE_LINE_CHARS = 12


def _norm_for_match(text: str) -> str:
    """Normalise for "did this text come from there?" comparisons.

    Punctuation and spacing are dropped because the model reflows them freely —
    it returned "possible!" for a source line reading "possible !", which is the
    same sentence and must not read as text from a different block.
    """
    return " ".join(re.sub(r"[^\w\s]", " ", text).split()).casefold()


def _content_matches_source(content: str, own_slice: str) -> bool:
    """True when *content* plausibly came from this block's own source slice.

    Compares distinctive lines against the slice rather than demanding an exact
    copy: the parser legitimately drops UI chrome, moves coaching out, and fills
    placeholder movements, so a block is judged by where the bulk of it came from.
    """
    haystack = _norm_for_match(_clean_block_text(own_slice))
    checked = [
        norm
        for line in _clean_block_text(content).splitlines()
        if len(norm := _norm_for_match(line)) >= _DISTINCTIVE_LINE_CHARS
    ]
    if not checked:
        return True  # nothing distinctive enough to judge — leave the block alone
    return sum(line in haystack for line in checked) / len(checked) >= 0.5


def _strip_foreign_lines(
    content: str, own: str, others: list[str], allow_empty: bool = False
) -> str:
    """Drop lines that belong to a different block's section.

    A block can be mostly right and still carry its neighbour's tail — enough of it
    is genuine that re-extracting the whole thing would be worse than trimming. A
    line is only removed on positive evidence: absent from this block's own section
    AND present in another's. Short lines are never removed; "+" and "3 Sets of :"
    recur everywhere and prove nothing.
    """
    own_hay = _norm_for_match(_clean_block_text(own))
    other_hays = [_norm_for_match(_clean_block_text(o)) for o in others]
    kept = [
        line
        for line in content.splitlines()
        if not (
            len(norm := _norm_for_match(line)) >= _DISTINCTIVE_LINE_CHARS
            and norm not in own_hay
            and any(norm in hay for hay in other_hays)
        )
    ]
    trimmed = "\n".join(kept).strip()
    # For a prescription, trimming everything means the evidence was misleading —
    # keep it whole rather than post an empty workout. A coaching note may legitimately
    # end up empty (plenty of blocks have none), so there the result stands.
    return trimmed if (trimmed or allow_empty) else content


def _verify_against_source(
    blocks: list[ProgrammingBlock], text: str, day_label: str, model: str
) -> list[ProgrammingBlock]:
    """Re-extract any block whose content came from a different block's section.

    When the model loses a block boundary it does not fail loudly — it hands one
    block's workout to its neighbour, which posts a plausible-looking but wrong
    session. Single-block recovery gets these right, so a block that fails the
    check is simply re-extracted.
    """
    slices = _source_slices(text)
    verified: list[ProgrammingBlock] = []
    for parsed in blocks:
        own = slices.get(_norm_title(parsed.name))
        if own is None:
            verified.append(parsed)
            continue

        block = parsed
        if not _content_matches_source(block.content, own):
            logger.warning(
                "%s: '%s' content does not come from its own section — re-extracting",
                day_label,
                block.name,
            )
            recovered = _recover_block(text, block.name, model)
            block = recovered if recovered is not None else block

        verified.append(block)
    return verified


def _trim_foreign_content(
    blocks: list[ProgrammingBlock], text: str, day_label: str
) -> list[ProgrammingBlock]:
    """Remove any other block's text from every block. Deterministic, no model call.

    Runs after recovery so it covers recovered blocks too — those are re-extracted
    from the raw text and can pick up a neighbour's tail just as the first pass can.
    """
    slices = _source_slices(text)
    trimmed_blocks: list[ProgrammingBlock] = []
    for block in blocks:
        own = slices.get(_norm_title(block.name))
        if own is None:
            trimmed_blocks.append(block)
            continue
        others = [s for key, s in slices.items() if key != _norm_title(block.name)]
        content = _strip_foreign_lines(block.content, own, others)
        # The coaching note gets the same treatment: a neighbour's level section
        # landing here is lifted into a difficulty level by _extract_levels, which
        # would offer a choice belonging to a different workout.
        instruction = _strip_foreign_lines(block.instruction, own, others, allow_empty=True)
        if (content, instruction) != (block.content, block.instruction):
            logger.warning("%s: trimmed another block's text out of '%s'", day_label, block.name)
        trimmed_blocks.append(block.replace(content=content, instruction=instruction))
    return trimmed_blocks


def _title_position(text: str, name: str) -> int:
    """Line number where *name* appears as a title in *text*; last if not found."""
    norm = _norm_title(name)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _norm_title(line) == norm:
            return i
    return len(lines)


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


# A difficulty-level section header standing alone on its line, after emoji have
# been stripped: "RX", "Rx :", "INTER+", "INTER -", "EMF - INTER +", and the
# qualifier form Strivee uses ("INTER (Je ne passes pas les RMU)"). Combined
# headers ("RX INTER", "Rx - INTER+ -") name several levels that share one
# prescription. A line carrying prescription text after the marker
# ("RX - 5 Ring Muscle-up") is NOT a header — those inline values are handled by
# _fill_placeholder_movements.
_LEVEL_TOKEN = r"INTER\s*\+|INTER|RX"
_LEVEL_HEADER_RE = re.compile(
    rf"^\s*(?:EMF\s*[-:]?\s*)?(?P<levels>(?:{_LEVEL_TOKEN})"
    rf"(?:\s*[-\u2013/&+,]?\s*(?:{_LEVEL_TOKEN}))*)"
    r"\s*[-\u2013:]?\s*(?:\([^)]*\))?\s*[-\u2013:]?\s*$",
    re.IGNORECASE,
)


def _header_levels(header: str) -> list[str]:
    """Level keys named by a header line, in order, de-duplicated."""
    keys: list[str] = []
    for m in re.finditer(_LEVEL_TOKEN, header, re.IGNORECASE):
        token = re.sub(r"\s+", "", m.group(0)).upper()
        key = {"INTER+": INTER_PLUS, "INTER": INTER, "RX": RX}[token]
        if key not in keys:
            keys.append(key)
    return keys


def _split_level_sections(text: str) -> tuple[str, list[tuple[list[str], str, str]]]:
    """Split *text* into its pre-header preamble and its per-level sections.

    Each section is ``(level keys, header line as written, body)``. The header is
    kept verbatim because Strivee writes the selection criteria into it
    ("EMF - INTER + (Je peux faire 1 Strict Muscle-up)"); a section that turns out
    to be advice rather than a prescription must go back untouched.
    """
    preamble: list[str] = []
    sections: list[tuple[list[str], str, list[str]]] = []
    for line in text.splitlines():
        m = _LEVEL_HEADER_RE.match(line) if line.strip() else None
        if m:
            sections.append((_header_levels(m.group("levels")), line.rstrip(), []))
        elif sections:
            sections[-1][2].append(line)
        else:
            preamble.append(line)
    return (
        "\n".join(preamble).strip(),
        [(levels, header, "\n".join(body).strip()) for levels, header, body in sections],
    )


def _norm_level_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


# Strivee writes the "which level am I?" criterion into the header itself
# ("EMF - INTER + (Je peux faire 1 Strict Muscle-up)"). That advice is worth keeping
# in the coaching note even once the prescription moves into its own field.
_QUALIFIER_RE = re.compile(r"\([^)]*\)")


def _levels_from_content(content: str, fields: dict[str, str], advice: list[str]) -> None:
    """Fill *fields* from level sections written inside content."""
    preamble, sections = _split_level_sections(content)
    if not sections:
        return
    advice.extend(h for _lv, h, body in sections if body and _QUALIFIER_RE.search(h))

    # A level's prescription is every section naming it, in source order: Strivee
    # writes shared work under a combined "RX INTER+ INTER" header and then a
    # per-level part below it, the two joined by a "+" line.
    collected: dict[str, list[str]] = {}
    for levels, _header, body in sections:
        if not body:
            continue
        for level in levels:
            collected.setdefault(level, []).append(body)
    found = {level: "\n".join(bodies) for level, bodies in collected.items()}
    if RX not in found and not preamble:
        return  # no identifiable RX prescription — leave the block as the model left it

    # Text above the first header is shared context ("Volume 4/4 -", a common format
    # line) and belongs to every level — but only when RX has a section of its own.
    # With no RX header that text IS the RX prescription, and prefixing it onto a
    # scaled variant would make the athlete do the harder work too.
    shared = preamble if RX in found else ""
    fields[RX] = f"{shared}\n{found[RX]}".strip() if RX in found else preamble
    for level in (INTER_PLUS, INTER):
        if not fields[level].strip() and level in found:
            fields[level] = f"{shared}\n{found[level]}".strip() if shared else found[level]


def _levels_from_instruction(instruction: str, fields: dict[str, str]) -> str:
    """Fill *fields* from level sections folded into instruction; return what's left."""
    preamble, sections = _split_level_sections(instruction)
    if not sections:
        return instruction
    kept = [preamble] if preamble else []
    for levels, header, body in sections:
        placed = []
        for level in (lv for lv in levels if lv != RX and body.strip()):
            if not fields[level].strip():
                fields[level] = body
                placed.append(level)
            elif _norm_level_text(fields[level]) == _norm_level_text(body):
                placed.append(level)  # the model already extracted it; this is a copy
        if placed:
            # The prescription lives in its own field now. Keep only the header, and
            # only when it carries selection advice — never a duplicate of the workout.
            if _QUALIFIER_RE.search(header):
                kept.append(header)
        else:
            # Advice-only sections, and levels we could not place, stay as written.
            kept.append(f"{header}\n{body}".strip() if body else header)
    return "\n\n".join(p for p in kept if p).strip()


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _drop_duplicate_paragraphs(instruction: str, fields: dict[str, str]) -> str:
    """Remove instruction paragraphs that merely repeat a level's prescription.

    The model sometimes emits a level into its own field AND leaves a copy in
    instruction, which would post every level into BTWB's coaching note.
    """
    known = {_norm_level_text(v) for v in fields.values() if v.strip()}
    return "\n\n".join(p for p in _paragraphs(instruction) if _norm_level_text(p) not in known)


# A lone "+" is Strivee's join between two parts of one workout. Left dangling at the
# end of content it means the model cut the prescription in half.
_CONTINUATION_RE = re.compile(r"^\s*\+\s*$")


def _rejoin_continuation(fields: dict[str, str], instruction: str) -> str:
    """Reattach a prescription the model split off after a dangling "+".

    Everything up to that "+" is work shared by all levels, so it is prefixed onto
    the scaled variants too — otherwise picking INTER would post only the second
    half of the session. Returns the instruction with the moved paragraph removed.
    """
    lines = fields[RX].rstrip().splitlines()
    if not lines or not _CONTINUATION_RE.match(lines[-1]):
        return instruction
    paragraphs = _paragraphs(instruction)
    if not paragraphs or _TRAILING_COACH_RE.match(paragraphs[0]):
        return instruction  # nothing to reattach, or what follows is coaching
    shared = fields[RX].rstrip()
    fields[RX] = f"{shared}\n{paragraphs[0]}"
    for level in (INTER_PLUS, INTER):
        if fields[level].strip():
            fields[level] = f"{shared}\n{fields[level].strip()}"
    return "\n\n".join(paragraphs[1:])


def _extract_levels(block: ProgrammingBlock) -> ProgrammingBlock:
    """Move per-level prescriptions out of content/instruction into their fields.

    The model is asked to emit ``inter_plus`` / ``inter`` directly but often leaves
    every level inside content (or folds them into instruction, as the pre-level
    prompt did), which would post the wrong workout — or all three at once. The
    headers are unambiguous once emoji are stripped, so the split is done here
    deterministically rather than trusted to the model.

    Levels the model already extracted win; only empty fields are filled. Content
    is left untouched when no RX prescription can be identified, since an empty
    content would drop the block entirely.
    """
    fields = {RX: block.content, INTER_PLUS: block.inter_plus, INTER: block.inter}
    advice: list[str] = []
    _levels_from_content(block.content, fields, advice)
    instruction = _levels_from_instruction(block.instruction, fields)
    # Strivee writes the coaching for a level inside that level's own section, so
    # it arrives glued to the prescription — and would be posted AS the workout
    # when that level is the one selected.
    for level, text in fields.items():
        fields[level], instruction = _resplit_trailing_coaching(text, instruction)
    instruction = _drop_duplicate_paragraphs(instruction, fields)
    instruction = _rejoin_continuation(fields, instruction)
    # Criteria lifted out of content join the coaching note, unless already there.
    for line in advice:
        if _norm_level_text(line) not in _norm_level_text(instruction):
            instruction = f"{instruction}\n\n{line}".strip() if instruction.strip() else line

    # A variant holding only a dangling sub-section label ("For quality -", "Skill :")
    # is leftover scaffolding, not a workout. Offering it as a choice would post the
    # label as the session.
    for level in (INTER_PLUS, INTER):
        body = fields[level].strip()
        if body and len(body.splitlines()) == 1 and body.endswith(("-", ":")):
            fields[level] = ""

    # An identical variant carries no choice — the source wrote one prescription
    # under a combined "RX INTER" header. Empty means "same as RX" downstream, and
    # keeping the copy would prompt for a decision that has no effect.
    for level in (INTER_PLUS, INTER):
        if fields[level].strip() == fields[RX].strip():
            fields[level] = ""

    return block.replace(
        content=fields[RX],
        inter_plus=fields[INTER_PLUS],
        inter=fields[INTER],
        instruction=instruction,
    )


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
        name=title,
        content=content,
        instruction=str(data.get("instruction", "")).strip(),
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

    # Check what survived the parse before filling gaps: a block holding its
    # neighbour's workout still counts as "present", so this has to run before the
    # missing-title pass or the wrong content is kept.
    blocks = _verify_against_source(blocks, text, day_label, model)

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

    # Recovered blocks are appended, so a day that needed recovery would otherwise
    # be posted out of order (BTWB lists blocks in the order they are created).
    # Sorting by where each title appears in the source restores the day as written.
    blocks.sort(key=lambda b: _title_position(text, b.name))

    blocks = _trim_foreign_content(blocks, text, day_label)

    # Deterministic final cleanup (the model is unreliable at all of this). Order
    # matters: strip emoji + Strivee UI chrome FIRST so the structural passes see
    # clean text (e.g. "🔱 RX - ..." must become "RX - ..." before the placeholder
    # fill can match it), then relocate trailing coaching, then fill "X ... Movement"
    # slots. Block names are already clean EMF/emoji-category titles — leave them.
    cleaned: list[ProgrammingBlock] = []
    for b in blocks:
        content = _fill_placeholder_movements(_clean_block_text(b.content))
        instruction = _clean_block_text(b.instruction)
        # _extract_levels both splits the levels and relocates each one's trailing
        # coaching, so coaching is NOT resplit before it: content now holds every
        # level, and splitting at the first "Objectif" line would cut the block
        # mid-way and sweep the levels below it into instruction.
        cleaned.append(_extract_levels(b.replace(content=content, instruction=instruction)))
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
