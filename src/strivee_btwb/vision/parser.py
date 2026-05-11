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

Everything between a block title and the next block title (or end of text) belongs to that block. A new block title ALWAYS starts a new block, even if the previous block's content looks topically related (e.g. a warm-up full of snatch drills followed by a real "EMF 60 : Snatch" block — these are TWO separate blocks, not one).

Critical example A — warm-up directly followed by an EMF block:
  Input lines:
    🔥 Warm-up 🔥
    KB Ankle Stretch x1min30/side
    Muscle Snatch from hip
    Hang Muscle Snatch
    Hang Power Snatch
    x6 reps / movement
    EMF 60 : Snatch
    Build to a 1RM Squat Snatch for the day
    Gamme suggéré : ...

  Correct interpretation:
    Block 1 = "🔥 Warm-up 🔥" (everything from KB Ankle Stretch through "x6 reps / movement") → DROPPED because Warm-up is excluded
    Block 2 = "EMF 60 : Snatch" (everything from "Build to a 1RM..." through Gamme suggéré...) → KEPT
  WRONG: merging the snatch drills into the EMF 60 : Snatch block. The drills belong to the warm-up only.

Critical example B — excluded block with long marketing content followed by a real block:
  Input lines:
    EMF 60 : Friday sport Simulation
    🎯 Classement directement disponible sur :
    https://strivee.app/marketplace/plan/...
    🥇 Depuis maintenant 5 ans, le FSS est devenu notre rendez-vous hebdomadaire incontournable pour tous les athlètes, coachs et boxes affiliées.
    ➡️ L'objectif n'a jamais été de simplement viser le haut du classement, mais bien de s'engager chaque semaine dans la pratique du fitness fonctionnel.
    💪🏻 Quelques conseils pour performer chaque vendredi : anticipez votre stratégie, entourez-vous (avec un juge si possible).
    EMF 60 - Easy Energy system
    Bike and Run -
    5 sets of :
    2min Bike erg #RPE 3-4
    2min Run #RPE 3-4
    ➡️ L'objectif ici est de bouger à basse intensité !

  Correct interpretation — TWO separate blocks:
    Block 1 = "EMF 60 : Friday sport Simulation" → DROPPED (Sport simulation is excluded). It ends the moment "EMF 60 - Easy Energy system" appears — regardless of how long its content was.
    Block 2 = "EMF 60 - Easy Energy system", content = "Bike and Run -\\n5 sets of :\\n2min Bike erg #RPE 3-4\\n2min Run #RPE 3-4", instruction = "Objectif : bouger à basse intensité !"
  WRONG: absorbing "EMF 60 - Easy Energy system" into the FSS block's content and dropping it alongside FSS. The block boundary rule is unconditional — it applies even when skipping an excluded block.

RULE OF THUMB: every line starting with "EMF " followed by a number/RX/Rx is its own block title and starts a new block. This applies unconditionally — even when the preceding block is being skipped. A skipped block ends at the very next EMF/emoji-category title line, just like any non-skipped block. There is never a case where an "EMF ..." title line should appear inside another block's content or instruction.

NOT block titles — these are sub-section headers WITHIN the current block, keep their text as part of the block content:
  • Lines ending with " -"  (e.g. "Warm-up -", "Main Part -", "Cooldown -", "Rest 3 min jogging between sets -")
  • Lines starting with "📌"  (e.g. "📌Échauffement", "📌Session")
  • Any other short label that does not match the EMF / emoji-category patterns above

━━━ CONTENT (prescription only) ━━━
"content" = the minimal statement of what to DO for the RX 🔱 level ONLY, copied exactly:
  • Never include the block title line itself in content
  • If the block has multiple difficulty levels (RX + INTER+ and/or INTER), content contains ONLY the RX prescription. NEVER concatenate RX with INTER+ or INTER content. The INTER+ / INTER prescriptions go to instruction (see DIFFICULTY LEVELS section).
  • Conditioning (AMRAP/EMOM/For Time/Run/Swim sets): time domain + movements + distances + reps + weights — for the RX section only
    — Include sub-section labels like "Warm-up -", "Main Part -", "Cooldown -" as structural markers in content
  • Strength (Build to / Find / Work to): ONLY the single goal sentence
    e.g. "Build to a 1RM Squat Snatch for the day"  ← that one line is the entire content
  • Do NOT include percentages-as-progressions, coaching explanations, or Objectif text in content

━━━ INSTRUCTION (everything else) ━━━
"instruction" = all coaching, guidance, and context — NOT the core RX prescription.
These markers ALWAYS begin an instruction section; move the marker line AND everything after it to instruction:
  • "Objectif"
  • "Gamme suggéré" / "Gammes suggéré"
  • "Tentatives lourdes"
  • "Si vous ratez" / "Si vous manquez"
  • "Score :"
  • "Compare" / "RPE" / "Niveau" / "Effort"
  Any sentence that explains, motivates, or coaches rather than prescribes → instruction.
  When in doubt → instruction.  If no instruction exists → use "".

PRESERVE FORMATTING: keep the original line breaks and blank lines from the source text inside instruction. Multi-paragraph instructions stay multi-paragraph (use \\n between lines, \\n\\n between sections).

━━━ DIFFICULTY LEVELS (RX vs INTER+ vs INTER) ━━━
Strivee blocks often present multiple difficulty levels:
  • RX 🔱 (or "🔱 Rx", "Rx 🔱", "RX 🔱") — the prescribed/main version
  • INTER+ 🪖 (or "🪖 INTER+ 🪖", "🪖 INTER+ -") — scaled intermediate-plus
  • INTER 🎖️ (or "🎖️ INTER 🎖️", "🎖️ INTER -") — scaled intermediate

How to map them:
  1. RX 🔱 prescription → "content". SKIP the RX header line itself; do not write "RX 🔱" anywhere.
  2. INTER+ 🪖 and INTER 🎖️ sections → "instruction". KEEP their header lines as visible section markers (e.g. "🪖 INTER+ 🪖", "🎖️ INTER -") and include their full prescription text below the header.
  3. Combined headers like "🔱 Rx - 🪖 INTER+ -" mean RX and INTER+ share the SAME content — put it once in "content", do NOT duplicate it in instruction. Only the separate "🎖️ INTER -" section (if present) goes to instruction.
  4. If only ONE level exists, its content goes to "content"; no level header anywhere.

Example with separate RX and INTER sections:
  Input:
    🔱 Rx
    AMRAP 12:00
    24 DU
    6 PC #50kg
    🎖️ INTER -
    AMRAP 12:00
    24 DU
    6 PC #40kg with abmat
    Objectif : Tenir les DU unbroken
  Output:
    "content":     "AMRAP 12:00\\n24 DU\\n6 PC #50kg"
    "instruction": "🎖️ INTER -\\nAMRAP 12:00\\n24 DU\\n6 PC #40kg with abmat\\n\\nObjectif : Tenir les DU unbroken"

Example with combined RX/INTER+ header:
  Input:
    🔱 Rx - 🪖 INTER+ -
    AMRAP 12:00
    24 DU
    🎖️ INTER -
    AMRAP 12:00
    20 DU with abmat
  Output:
    "content":     "AMRAP 12:00\\n24 DU"
    "instruction": "🎖️ INTER -\\nAMRAP 12:00\\n20 DU with abmat"

Example with all three levels separate (THIS IS THE COMMON CASE — pay close attention):
  Input:
    RX 🔱
    AMRAP 12:00
    5 Ring Muscle-up
    8 Strict HSPU
    15 Toes to Bar
    200m Run
    INTER+ 🪖
    AMRAP 12:00
    4 Bar Muscle-up
    6 Strict HSPU
    12 Toes to Bar
    200m Run
    INTER 🎖️
    AMRAP 12:00
    5 Chest to bar pull-up
    6 Strict HSPU Abmat
    10 Toes to Bar
    200m Run
    Objectif : Unbroken sur chaque mouvement gym
  Output:
    "content":     "AMRAP 12:00\\n5 Ring Muscle-up\\n8 Strict HSPU\\n15 Toes to Bar\\n200m Run"
    "instruction": "🪖 INTER+ 🪖\\nAMRAP 12:00\\n4 Bar Muscle-up\\n6 Strict HSPU\\n12 Toes to Bar\\n200m Run\\n\\n🎖️ INTER 🎖️\\nAMRAP 12:00\\n5 Chest to bar pull-up\\n6 Strict HSPU Abmat\\n10 Toes to Bar\\n200m Run\\n\\nObjectif : Unbroken sur chaque mouvement gym"

  WRONG output (do NOT do this):
    "content": "AMRAP 12:00\\n5 Ring Muscle-up...\\n\\nAMRAP 12:00\\n4 Bar Muscle-up...\\n\\nAMRAP 12:00\\n5 Chest to bar pull-up..."  ← all 3 levels in content is FORBIDDEN

━━━ IGNORE COMPLETELY ━━━
- Day-tab labels: LUN, MAR, MER, JEU, VEN, SAM, DIM and Mon/Tue/Wed/Thu/Fri/Sat/Sun
- Lines that are a single number 1-31 (date numbers in the week strip)
- App header lines: lines that are EXACTLY "EMF 60'" or "EMF 45'" (with a minute/prime symbol ') — NOT block titles like "EMF 60 : Snatch" which have a colon and a name after them
- Bottom nav tabs: WOD, Box, Noter, PRs, Profil
- Lines matching "N Scores", "N Score", "N Media", "N Media" where N is a number
- Lines starting with http:// or https://
- Announcements: WhatsApp groups, Zoom/Meet calls, weekly call banners
- SKIP any block whose title (the EMF or emoji+category header line) contains: {excluded} — skip ONLY that individual block; do not stop processing, the next block title starts a new block normally. IMPORTANT: the exclusion check applies only to block title lines — never to content or instruction lines inside a block (e.g. "Warm-up : 60% / 70%..." is a percentage label inside a Gamme suggéré, NOT a new block to exclude)
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
