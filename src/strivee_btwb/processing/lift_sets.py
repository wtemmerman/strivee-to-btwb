"""Read a single-movement set scheme for BTWB's classic "Sets" template.

BTWB's AI generator refuses some plain set schemes outright — "2 sets of : Max rep
strict HSPU with Abmat" came back with no preview however often it was asked —
while its classic Sets template takes them directly. Only a block that is nothing
but the scheme is read; anything more stays on the AI path.
"""

import re

from ..core.models import ClassicSets, ProgrammingBlock

_SETS_HEADER_RE = re.compile(r"^\s*(\d+)\s*sets?\s*(?:of)?\s*:?\s*$", re.IGNORECASE)
_MAX_REP_RE = re.compile(r"^\s*max\s*reps?\s+(.+?)\s*$", re.IGNORECASE)
_REST_RE = re.compile(
    r"^\s*-?\s*rest\s+(\d+)\s*(min(?:ute)?s?|'|sec(?:ond)?s?|s)(?![a-z])"
    r"(?:\s+between\s+sets?)?\s*-?\s*$",
    re.IGNORECASE,
)
# The aid a movement is scaled with is coaching, not the movement: BTWB has
# "Strict Handstand Push-up", the note keeps "with Abmat".
_AID_RE = re.compile(r"\s+(?:with|avec)\s+.*$", re.IGNORECASE)
# Coaching qualifiers, not part of any BTWB movement name.
_QUALIFIER_RE = re.compile(r"\b(?:unbroken|ub)\b", re.IGNORECASE)
_PHRASES = [
    (re.compile(r"\bchest[\s-]+to[\s-]+bar\b", re.IGNORECASE), "Chest-to-bar"),
    (re.compile(r"\btoes[\s-]+to[\s-]+bar\b", re.IGNORECASE), "Toes-to-bar"),
]
_ABBREVIATIONS = {
    "hspu": "Handstand Push-up",
    "c2b": "Chest-to-bar Pull-up",
    "t2b": "Toes-to-bar",
    "ttb": "Toes-to-bar",
    "du": "Double Under",
    "dus": "Double Unders",
    "rmu": "Ring Muscle-up",
    "bmu": "Bar Muscle-up",
}


def btwb_movement_name(text: str) -> str:
    """The BTWB name a Strivee movement is written as: aids dropped, abbreviations expanded.

    Posting looks the result up by exact name and skips the block, reported for
    adding by hand, when BTWB has no such movement: a wrong guess never posts the
    wrong movement.
    """
    text = _QUALIFIER_RE.sub("", _AID_RE.sub("", text))
    for pattern, name in _PHRASES:
        text = pattern.sub(name, text)
    words = [_ABBREVIATIONS.get(w.lower(), w) for w in text.split()]
    # Shouted words ("RING") are written as BTWB spells names; expansions stay as given.
    words = [w.capitalize() if w.isupper() and len(w) > 1 else w for w in words]
    return " ".join(w[:1].upper() + w[1:] for w in " ".join(words).split())


def _seconds(value: str, unit: str) -> int:
    return int(value) * (1 if unit.lower().startswith("s") else 60)


def classic_sets(block: ProgrammingBlock) -> ClassicSets | None:
    """The block as "N sets of max reps of one movement", or None if it is anything else."""
    lines = [line for line in block.content.splitlines() if line.strip()]
    if len(lines) not in (2, 3):
        return None
    header, movement = _SETS_HEADER_RE.match(lines[0]), _MAX_REP_RE.match(lines[1])
    rest = _REST_RE.match(lines[2]) if len(lines) == 3 else None
    if not header or not movement or (len(lines) == 3 and not rest):
        return None
    return ClassicSets(
        movement=btwb_movement_name(movement.group(1)),
        reps=(None,) * int(header.group(1)),
        rest_seconds=_seconds(rest.group(1), rest.group(2)) if rest else None,
    )


def describe_sets(plan: ClassicSets) -> str:
    """What preview shows for a classic Sets block."""
    reps = ", ".join("max" if r is None else str(r) for r in plan.reps)
    seconds = plan.rest_seconds
    rest = "rest as needed" if seconds is None else f"rest {seconds // 60}:{seconds % 60:02d}"
    return f"{plan.movement} - Sets\n{len(plan.reps)} sets: {reps} reps, {rest}"
