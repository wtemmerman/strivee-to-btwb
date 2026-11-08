"""Apply a level published only as a scale line to the RX workout.

Strivee sometimes publishes a scaled level as nothing but "Scale - 6 Bar muscle-up
-> 3 Bar Muscle-up -> 8 Burpees bar muscle-up": a chain from the RX movement to its
easier versions. Posted as is, BTWB got a one-line workout with no structure, so
the 10-19 and 10-26 Mixed Modals were rewritten by hand. The RX workout with the
chain's first step swapped in is what that level actually does.
"""

import re

# "Scale - 6 Bar muscle-up -> 3 Bar Muscle-up -> 8 Burpees bar muscle-up"
_SCALE_RE = re.compile(r"^\s*scale\s*[-:]\s*(.+?->.+?)\s*$", re.IGNORECASE)
# "6 Bar muscle-up", and an RX line "10-6 Bar Muscle-up" (men / women)
_COUNTED_RE = re.compile(r"^\s*(\d+(?:\s*[-/]\s*\d+)?)\s+(.+?)\s*$")


def _movement(text: str) -> str | None:
    m = _COUNTED_RE.match(text)
    return re.sub(r"\s+", " ", m.group(2)).lower() if m else None


def apply_scale(rx: str, level: str) -> str | None:
    """*rx* with each scale chain's first step applied, or None when *level* is not that.

    Only a level made of nothing but chains is applied, and only when each chain's
    movement is on exactly one RX line: anything else is left as published.
    """
    chains: list[str] = []
    for line in (line for line in level.splitlines() if line.strip()):
        if not (found := _SCALE_RE.match(line)):
            return None
        chains.append(found.group(1))
    if not chains:
        return None
    lines = rx.splitlines()
    for chain in chains:
        steps = [step.strip() for step in chain.split("->")]
        movement = _movement(steps[0])
        at = [i for i, line in enumerate(lines) if movement and _movement(line) == movement]
        if len(at) != 1 or _movement(steps[1]) is None:
            return None
        lines[at[0]] = steps[1]
    return "\n".join(lines)
