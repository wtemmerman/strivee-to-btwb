"""Per-muscle set counting for the accessory audit.

Answers one question: for each muscle the accessory pool exists to train, how
many hard sets did this week's EMF programming already provide? The gap between
that and the pool's weekly target is what the accessory work has to fill.

The arithmetic lives here, in plain Python, and nowhere else. Reading messy
coach shorthand into a list of sets is a language problem and belongs to the
model (:mod:`.set_extract`); deciding what a set is *worth* is a judgement that
has to stay inspectable and testable, so it is a table.

The judgement that matters is the credit weighting. A twenty-minute metcon
containing sixty pull-ups is limited by breathing, not by the lats, and counting
it as sixty pull-up sets would report every muscle as covered every week. So
conditioning earns a quarter-credit per set and is capped per muscle per week;
without that cap CrossFit volume drowns out the signal entirely.

The cap does second duty as the ceiling on extraction error. The two failure
directions are not symmetric: over-crediting tells the athlete to skip work they
need, while under-crediting only adds a set they did not strictly have to do. So
anything the model reads unreliably is routed through conditioning, where a wrong
set count costs at most METCON_CAP. EMOM blocks are the live example — qwen3:8b
will not reliably split "EMOMx12" across the three movements rotating through it,
so they are counted as conditioning rather than trusted as strength work.
"""

import json
import logging
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from ..core import config

logger = logging.getLogger("processing")

# ── Block types ───────────────────────────────────────────────────────────────

STRENGTH = "strength"
"""Sets of 3+ reps against a load, taken close to failure."""

HEAVY_SINGLE = "heavy_single"
"""1-2 reps, or 'build to a heavy X'. Neural work — negligible volume."""

GYMNASTICS_MAX = "gymnastics_max"
"""A bodyweight set taken to failure, e.g. 'max rep strict bar muscle-up'."""

ACCESSORY = "accessory"
"""Isolation work prescribed for a single muscle."""

SKILL = "skill"
"""Technique or 'for quality' work, deliberately stopped short of failure."""

METCON = "metcon"
"""Conditioning: for time, AMRAP, EMOM, intervals."""

BLOCK_TYPES: tuple[str, ...] = (STRENGTH, HEAVY_SINGLE, GYMNASTICS_MAX, ACCESSORY, SKILL, METCON)

SET_CREDIT: dict[str, float] = {
    STRENGTH: 1.0,
    GYMNASTICS_MAX: 1.0,
    ACCESSORY: 1.0,
    METCON: 0.25,
    HEAVY_SINGLE: 0.0,
    SKILL: 0.0,
}
"""How much one prescribed set of each block type counts as a hypertrophy set.

Zero for heavy singles and skill work is deliberate, not an oversight: a top
single is one rep of mechanical tension, and quality work stops far from
failure. Neither grows muscle, so neither should let the audit skip a set.
"""

METCON_CAP = 2.0
"""Ceiling on what conditioning may credit to one muscle in one week.

Even at a quarter-credit, a week of high-rep metcons would otherwise clear every
target on its own. The cap says conditioning can contribute up to a maintenance
dose to a muscle and no more, which matches how it actually loads them.
"""


# ── Data tables ───────────────────────────────────────────────────────────────

# Loads and percentages carry "/" ("#50/35kg"), so they have to go before the
# alternatives split or "Clean and jerk #50/35kg" truncates to "Clean and jerk #50".
_LOAD = re.compile(r"[#@]\s*[\d.,%/-]+\s*(?:kg|lbs?|%)?", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalise(movement: str) -> str:
    """Reduce a movement name to a lookup key: lowercase words, single-spaced.

    Punctuation is dropped rather than matched so that the table can be written
    the readable way ("Bar Muscle-up") and still match whatever spelling the
    model returns ("bar muscle up").
    """
    text = _LOAD.sub(" ", movement.lower())
    # Alternatives are written with spaces around the slash ("Bar Muscle-up / CTB");
    # a bare slash belongs to the name ("A/R ramp handstand walk"), and splitting on
    # it truncated that movement to "a".
    text = text.split(" / ")[0]
    return " ".join(_NON_ALNUM.sub(" ", text).split())


@lru_cache(maxsize=1)
def _pool() -> dict:
    return json.loads((config.DATA_DIR / "exercise_pool.json").read_text(encoding="utf-8"))


def pool_muscles() -> dict[str, dict]:
    """The accessory pool keyed by muscle, as published in ``exercise_pool.json``."""
    return _pool()["muscles"]


def pool_movements(muscle: str, location: str) -> list[dict]:
    """Pool movements for *muscle* that can be performed at *location*."""
    return [m for m in pool_muscles()[muscle]["movements"] if location in m["locations"]]


@lru_cache(maxsize=1)
def _muscle_map() -> tuple[dict[str, dict[str, float]], tuple[str, ...]]:
    """The movement→muscle table, normalised, plus its keys longest-first."""
    raw = json.loads((config.DATA_DIR / "movement_muscles.json").read_text(encoding="utf-8"))
    table = {_normalise(name): weights for name, weights in raw["movements"].items()}
    unknown = {m for weights in table.values() for m in weights} - set(pool_muscles())
    if unknown:
        raise ValueError(
            f"movement_muscles.json credits muscles absent from exercise_pool.json: "
            f"{sorted(unknown)}. The two files must agree on muscle keys."
        )
    return table, tuple(sorted(table, key=len, reverse=True))


def muscles_for(movement: str) -> dict[str, float] | None:
    """Return the muscles *movement* trains and by how much, or None if unlisted.

    None means "not in the table", which the caller reports rather than treats as
    zero — an uncredited movement that should have counted is a table gap, and
    silently scoring it zero would hide exactly the case worth seeing.
    """
    table, longest_first = _muscle_map()
    key = _normalise(movement)
    if not key:
        return None
    if key in table:
        return table[key]
    # Most specific wording wins: "strict pull up" contains "pull up", and the
    # strict version is worth twice as much, so it has to be tested first.
    for candidate in longest_first:
        if candidate in key:
            return table[candidate]
    return None


# ── Counting ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WorkSet:
    """One movement inside a block, with how many sets of it the block prescribes."""

    movement: str
    sets: int
    reps: str = ""
    block_type: str = METCON
    source: str = ""  # block name, carried for the report


@dataclass(frozen=True)
class MuscleVolume:
    """What one week of programming credited to one muscle."""

    muscle: str
    label: str
    target: float
    direct: float  # from strength / gymnastics / accessory sets
    metcon: float  # from conditioning, after METCON_CAP
    metcon_raw: float  # what conditioning would have credited uncapped
    sources: tuple[tuple[str, float], ...]  # (movement, credit), largest first

    @property
    def credited(self) -> float:
        return self.direct + self.metcon

    @property
    def gap(self) -> float:
        return max(0.0, self.target - self.credited)


def weekly_volume(
    work_sets: Iterable[WorkSet],
) -> tuple[dict[str, MuscleVolume], list[str]]:
    """Credit *work_sets* to the pool's muscles.

    Returns the per-muscle volumes and, separately, the movements that are not in
    the table. The metcon cap is applied per muscle across the whole week, so this
    has to see the full week at once — capping block by block would let six
    conditioning blocks each contribute a capped share.
    """
    direct: dict[str, float] = defaultdict(float)
    metcon_raw: dict[str, float] = defaultdict(float)
    contributions: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    unlisted: dict[str, None] = {}  # insertion-ordered set

    for work_set in work_sets:
        weights = muscles_for(work_set.movement)
        if weights is None:
            unlisted.setdefault(work_set.movement.strip(), None)
            continue
        # A block_type outside SET_CREDIT means set_extract let an invalid value
        # through; raise rather than score it as zero and under-report the week.
        credit = work_set.sets * SET_CREDIT[work_set.block_type]
        if not credit:
            continue
        bucket = metcon_raw if work_set.block_type == METCON else direct
        for muscle, weight in weights.items():
            bucket[muscle] += credit * weight
            contributions[muscle][work_set.movement] += credit * weight

    volumes = {}
    for muscle, spec in pool_muscles().items():
        volumes[muscle] = MuscleVolume(
            muscle=muscle,
            label=spec["label"],
            target=float(spec["weekly_target_sets"]),
            direct=direct[muscle],
            metcon=min(metcon_raw[muscle], METCON_CAP),
            metcon_raw=metcon_raw[muscle],
            sources=tuple(sorted(contributions[muscle].items(), key=lambda kv: -kv[1])),
        )
    return volumes, list(unlisted)
