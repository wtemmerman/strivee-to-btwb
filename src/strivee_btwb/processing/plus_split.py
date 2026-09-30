"""Split a block at Strivee's joins into separate BTWB workouts.

A "+", "Into" or "Then" on its own line joins two pieces of work in one Strivee
block ("3 sets of negatives + 2 sets of max reps", "bike sprints Into row
sets"). BTWB's workout generator reads the whole block as one workout and
mangles or refuses it, and the formatter drops one side of an "Into", so each
piece is formatted and posted on its own.

Cardio blocks use the same join around a warm-up and a cooldown. Those are not
work to log, so they move to the coaching note and only the main set is posted.
"""

import re

from ..core.models import ProgrammingBlock
from .timing import COOLDOWN_RE, WARM_UP_RE

_JOIN_LINE_RE = re.compile(r"^\s*(\+|into|then)\s*$", re.MULTILINE | re.IGNORECASE)
# A join can also sit inside one round: "4 sets, each for time of : / 30 sec hold /
# Into / 15m Handstand walk / - Rest 1min between sets -" is one workout.
_ROUNDS_HEADER_RE = re.compile(r"^\s*\d+\s*(?:sets?|rounds?)\b.*(?:of|:)\s*$", re.IGNORECASE)
_REST_BETWEEN_RE = re.compile(r"\brest\b.*\bbetween\s+(?:sets?|rounds?)\b", re.IGNORECASE)
# "Accumulated 8 Reps /movement" over a list of drills: technique work ahead of the
# main piece, with no movement BTWB knows — the AI filled one with snatches.
_DRILLS_RE = re.compile(r"^\s*accumulated\b.*\bmovement\b", re.IGNORECASE)
# The bike's cooldown never says so; it ramps down instead: "5min #65 to 40% FTP20".
_RANGE_RE = re.compile(r"(\d+)\s*(?:to|à|-)\s*(\d+)\s*%", re.IGNORECASE)


_PART_SUFFIX_RE = re.compile(r"\s*\(\d+/\d+\)$")


def unsplit_name(name: str) -> str:
    """The block title a split part came from: "Handstand walk (1/2)" → "Handstand walk"."""
    return _PART_SUFFIX_RE.sub("", name)


def _inside_open_round(before: str, after: str) -> bool:
    """Whether a join between *before* and *after* falls inside one round.

    True when *before* opens a sets/rounds scheme it never closes with its rest
    line, and that rest line comes after the join.
    """
    lines = before.splitlines()
    headers = [i for i, line in enumerate(lines) if _ROUNDS_HEADER_RE.match(line)]
    if not headers:
        return False
    if any(_REST_BETWEEN_RE.search(line) for line in lines[headers[-1] :]):
        return False
    return bool(_REST_BETWEEN_RE.search(after))


def _joined_parts(content: str) -> list[str]:
    pieces = _JOIN_LINE_RE.split(content)
    parts = [pieces[0]]
    for join, part in zip(pieces[1::2], pieces[2::2]):
        if _inside_open_round(parts[-1], part):
            parts[-1] = f"{parts[-1].rstrip()}\n{join}\n{part.lstrip()}"
        else:
            parts.append(part)
    return [p.strip() for p in parts if p.strip()]


def is_lead_in(paragraph: str) -> bool:
    """Whether a note paragraph is a warm-up or drill list the split moved there.

    Those come before the work they lead into, so the note keeps them first.
    """
    return bool(WARM_UP_RE.search(paragraph) or _DRILLS_RE.match(paragraph))


def _is_ramp_down(part: str) -> bool:
    return any(int(high) > int(low) for high, low in _RANGE_RE.findall(part))


def _labelled(part: str, label: str, names_itself: re.Pattern[str]) -> str:
    return part if names_itself.search(part) else f"{label} :\n{part}"


def split_plus_joins(block: ProgrammingBlock) -> list[ProgrammingBlock]:
    """The workouts *block* posts as: one per joined part of its content.

    Only a leading warm-up and a trailing cooldown move to the note — a part in
    the middle is work whatever it mentions.
    """
    parts = _joined_parts(block.content)
    if len(parts) < 2:
        return [block]

    warm_ups: list[str] = []
    while len(parts) > 1 and (WARM_UP_RE.search(parts[0]) or _DRILLS_RE.match(parts[0])):
        warm_ups.append(parts.pop(0))
    cooldowns: list[str] = []
    while parts and (COOLDOWN_RE.search(parts[-1]) or _is_ramp_down(parts[-1])):
        cooldowns.insert(0, parts.pop())
    if not parts:
        return [block]  # nothing left that is work — post it as published

    note = "\n\n".join(
        [p if _DRILLS_RE.match(p) else _labelled(p, "Warm-up", WARM_UP_RE) for p in warm_ups]
        + ([block.instruction] if block.instruction.strip() else [])
        + [_labelled(p, "Cooldown", COOLDOWN_RE) for p in cooldowns]
    )
    if len(parts) == 1:
        return [block.replace(content=parts[0], instruction=note)]
    return [
        block.replace(
            name=f"{block.name} ({i}/{len(parts)})",
            content=part,
            instruction=note if i == 1 else "",
        )
        for i, part in enumerate(parts, start=1)
    ]
