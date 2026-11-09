"""Apply a level published only as changes to the RX workout.

Strivee sometimes publishes a scaled level as nothing but what differs from RX:
a scale chain from the RX movement to its easier versions ("Scale - 6 Bar muscle-up
-> 3 Bar Muscle-up -> 8 Burpees bar muscle-up"), or the scaled lines joined on one
line ("400/300m Row - 20 Thruster #15/10kg - 10 Pull-up (Banded or Jumping)").
Posted as is, BTWB got a one-line workout with no structure, so the 10-19, 10-26,
11-04 and 11-11 levels were rewritten by hand. The RX workout with those lines
swapped in is what that level actually does.
"""

import re

# "Scale - 6 Bar muscle-up -> 3 Bar Muscle-up -> 8 Burpees bar muscle-up"
_SCALE_RE = re.compile(r"^\s*scale\s*[-:]\s*(.+?->.+?)\s*$", re.IGNORECASE)
# The pieces of "Same as RX - 25 Thruster #15/10kg - 15 Pull-up"; "Pull-up" has no spaces.
_PIECE_SEPARATOR_RE = re.compile(r"\s+[-\u2013]\s+")
_SAME_AS_RX_RE = re.compile(r"^\s*same\s+as\s+rx\s*$", re.IGNORECASE)
# "10-6 Bar Muscle-up" (men / women), "500/400m Row", "20/16 Calories Row"
_QUANTITY_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?(?:\s*[-/]\s*\d+(?:[.,]\d+)?)*\s*(?:k?m|cal(?:orie)?s?|reps?)?(?![a-z])\s*",
    re.IGNORECASE,
)
# What follows the movement — "#20/15kg", "@70%", "(Banded or Jumping)" — differs by level.
_LOAD_OR_CUE_RE = re.compile(r"\s*[#@(].*$")
# "RX - Target : Sub 4:00" belongs to the level being replaced.
_RX_HEADER_RE = re.compile(r"^\s*RX\b")


def _movement(text: str) -> str | None:
    """The movement a counted line names, with its quantity, load and cues dropped."""
    m = _QUANTITY_RE.match(text)
    if not m or not m.group(0).strip():
        return None
    name = _LOAD_OR_CUE_RE.sub("", text[m.end() :])
    return re.sub(r"\s+", " ", name).strip().lower() or None


def _swaps(level: str) -> list[tuple[str, str]] | None:
    """(the RX line's movement, the line replacing it) for each change *level* makes.

    None when *level* is anything but changes: every line a scale chain, or one
    line of two or more counted pieces, optionally opening with "Same as RX".
    """
    lines = [line for line in level.splitlines() if line.strip()]
    if not lines:
        return None
    chains = [m.group(1).split("->") for line in lines if (m := _SCALE_RE.match(line))]
    if len(chains) == len(lines):
        pairs = [(steps[0].strip(), steps[1].strip()) for steps in chains]
    elif len(lines) == 1:
        pieces = _PIECE_SEPARATOR_RE.split(lines[0].strip())
        same_as_rx = bool(_SAME_AS_RX_RE.match(pieces[0]))
        pieces = pieces[1:] if same_as_rx else pieces
        if not pieces or (len(pieces) < 2 and not same_as_rx):
            return None
        pairs = [(piece, piece) for piece in pieces]
    else:
        return None
    swaps = []
    for old, new in pairs:
        movement = _movement(old)
        if movement is None or _movement(new) is None:
            return None
        swaps.append((movement, new))
    return swaps


def apply_to_rx(rx: str, level: str) -> str | None:
    """*rx* with *level*'s changes swapped in, or None when *level* is not only changes.

    Each change must name the movement of exactly one RX line, and no two the same
    line: anything else is left as published.
    """
    swaps = _swaps(level)
    if swaps is None:
        return None
    lines = rx.splitlines()
    replaced: set[int] = set()
    for movement, new in swaps:
        at = [i for i, line in enumerate(lines) if _movement(line) == movement]
        if len(at) != 1 or at[0] in replaced:
            return None
        lines[at[0]] = new
        replaced.add(at[0])
    return "\n".join(line for line in lines if not _RX_HEADER_RE.match(line))
