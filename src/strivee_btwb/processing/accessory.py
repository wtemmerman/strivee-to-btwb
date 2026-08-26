"""Turn the audit's per-muscle gap into the accessory block to post.

Entirely deterministic, and deliberately so. The pool already stores BTWB's own
movement names, so the text this builds is in BTWB's vocabulary before it is
written — there is nothing here for the format model to improve and a great deal
for it to break. This block must never be routed through ``llm_format_week``.
"""

import logging
import math
import re
from dataclasses import dataclass

from ..core.models import ProgrammingBlock
from .volume import MuscleVolume, pool_movements

logger = logging.getLogger("processing")

ACCESSORY_BLOCK_NAME = "Accessory"
"""Title every accessory block carries, on every date.

Constant on purpose: BTWB's duplicate check matches on the block title, so a
stable name makes re-posting a no-op. A title that described the muscles would
change whenever the week's gap changed, and posting again would then add a second
block beside the stale one instead of skipping it.
"""

SPLIT_ACROSS_TWO_MOVEMENTS = 4
"""Sets from which a muscle is worked with two movements rather than one.

Six straight sets of the same calf raise is both dull and incomplete — the pool
pairs a straight-knee movement with a bent-knee one precisely because they train
different heads. Below this many sets there is nothing worth splitting.
"""


_REP_RANGE = re.compile(r"^\s*(\d+)\s*-\s*\d+\s*$")


def target_reps(reps: str) -> str:
    """Collapse a published rep range to the single number to train against.

    Every set here goes to failure, so the rep target's only job is to fix the
    load: hit the number, and when you clear it, add weight. A range leaves that
    ambiguous — "12-15" does not say when to load up, "12" does. The pool keeps
    the range because it still documents where a movement belongs; the
    prescription takes the bottom of it.
    """
    match = _REP_RANGE.match(reps)
    return match.group(1) if match else reps.strip()


@dataclass(frozen=True)
class Prescription:
    """One movement in an accessory block, and how much of it to do."""

    muscle: str
    label: str
    btwb_name: str
    sets: int
    reps: str


def _distinct_options(muscle: str, location: str) -> list[dict]:
    """Pool movements for *muscle* at *location*, one per BTWB name."""
    by_name: dict[str, dict] = {}
    for movement in pool_movements(muscle, location):
        by_name.setdefault(movement["btwb_name"], movement)
    return list(by_name.values())


def _prescribe(volume: MuscleVolume, location: str) -> list[Prescription]:
    """Choose the movements and set split that close one muscle's gap."""
    options = _distinct_options(volume.muscle, location)
    if not options:
        logger.warning("No %s option in the pool for %s — skipping it", location, volume.label)
        return []
    sets = math.ceil(volume.gap)
    if sets >= SPLIT_ACROSS_TWO_MOVEMENTS and len(options) > 1:
        first = sets - sets // 2
        chosen = [(options[0], first), (options[1], sets - first)]
    else:
        chosen = [(options[0], sets)]
    return [
        Prescription(volume.muscle, volume.label, m["btwb_name"], n, target_reps(m["reps"]))
        for m, n in chosen
    ]


def assign_to_days(volumes: dict[str, MuscleVolume], day_labels: list[str]) -> dict[str, list]:
    """Spread the muscles that are short across *day_labels*, balancing set count.

    Whole muscles move together rather than being sliced across days: two sets of
    lateral raises on Tuesday and three on Friday is worse training than five in
    one session, and it doubles the movements each session has to set up.
    """
    short = sorted((v for v in volumes.values() if v.gap > 0), key=lambda v: (-v.gap, v.muscle))
    load = dict.fromkeys(day_labels, 0)
    plan: dict[str, list[MuscleVolume]] = {label: [] for label in day_labels}
    for volume in short:
        # Tie-break on the requested order so the same week always plans the same way.
        target = min(day_labels, key=lambda label: (load[label], day_labels.index(label)))
        plan[target].append(volume)
        load[target] += math.ceil(volume.gap)
    return plan


def plan_accessory(
    volumes: dict[str, MuscleVolume], location: str, day_labels: list[str]
) -> dict[str, list[Prescription]]:
    """Return the movements to do on each requested day to close the week's gap."""
    return {
        label: [p for volume in muscles for p in _prescribe(volume, location)]
        for label, muscles in assign_to_days(volumes, day_labels).items()
    }


def build_block(entries: list[Prescription]) -> ProgrammingBlock:
    """Render one day's prescriptions as the block BTWB will receive.

    One line per SET, not per movement, even though that repeats the same line
    three times. BTWB gives a written round its own single load field, so
    "3 sets of 12 Cable Lateral Raise" can only ever record one weight for all
    three. Written out, each set gets its own row to log a load and a rep count
    into — which is the whole record when the sets go to failure and the load is
    what is being progressed.
    """
    content = "\n".join(f"{e.reps} {e.btwb_name}" for e in entries for _ in range(e.sets))
    targets = ", ".join(dict.fromkeys(e.label for e in entries))
    instruction = (
        "Accessory work balancing what this week's CrossFit programming left untrained.\n"
        f"Targets: {targets}.\n"
        "Take every set to failure — the volume is this low because the effort is high.\n"
        "The rep number sets the load: pick a weight that fails you there, and go up "
        "once you clear it on every set.\n"
        "Rest 60-90s, or superset two movements that do not share a muscle."
    )
    return ProgrammingBlock(name=ACCESSORY_BLOCK_NAME, content=content, instruction=instruction)
