"""Strivee movement wording → the name BTWB recognises.

BTWB only links a workout line to a movement (and therefore to history, PRs and
leaderboards) when the name matches its own vocabulary. Strivee writes the same
movements in coach shorthand, so a faithful copy of the programming can still
land as unrecognised text. Each entry below was confirmed against BTWB while
posting a real week — add to it whenever BTWB fails to resolve a movement.

Written as (pattern, replacement) rather than a dict because order matters: the
most specific wording has to win.

An alias only helps if BTWB's AI generator accepts the result. "Single arm Wall
facing handstand Hold" used to become "Wall Facing Handstand Hold Weight Shift",
which the generator refused every time it was sent (Sat 10-10, Sat 10-17); the
Strivee wording posts and reads back clean.
"""

import re

# (Strivee wording, BTWB movement). Matched case-insensitively on whole words.
# Qualifiers BTWB expresses as attributes rather than names — "Weighted", rep
# counts, loads — are left in place around the movement.
MOVEMENT_ALIASES: tuple[tuple[str, str], ...] = (
    # 2026-08-19 / 2026-08-21: BTWB calls every eccentric RMU a "negative", with
    # no "strict" in the name. "Eccentric strict RMU" is the same movement.
    ("Negative strict Ring Muscle-up", "Negative Ring Muscle-up"),
    ("Eccentric strict Ring Muscle-up", "Negative Ring Muscle-up"),
    ("Eccentric strict RMU", "Negative Ring Muscle-up"),
    # 2026-08-21: "#50% ... Weighted strict chest to ring" — BTWB's movement is the
    # pull-up; "Weighted" stays in front of it as the qualifier.
    ("strict chest to ring", "Strict Chest-to-Ring Pull-up"),
)

_COMPILED = tuple(
    (re.compile(rf"\b{re.escape(strivee)}\b", re.IGNORECASE), btwb)
    for strivee, btwb in MOVEMENT_ALIASES
)


def apply_movement_aliases(text: str) -> str:
    """Rewrite Strivee movement wording to the names BTWB resolves."""
    for pattern, btwb in _COMPILED:
        text = pattern.sub(btwb, text)
    return text
