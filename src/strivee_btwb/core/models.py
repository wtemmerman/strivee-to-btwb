"""Shared data models for the strivee-btwb pipeline.

The models are frozen: every pipeline stage produces a new value rather than
mutating in place (capture → parse → clean → format → post), so immutability
matches how the data actually flows and rules out a stage accidentally editing a
cached object. Use :meth:`ProgrammingBlock.replace` to derive an edited copy.
"""

from dataclasses import dataclass, field, replace
from datetime import date

RX = "rx"
INTER_PLUS = "inter_plus"
INTER = "inter"

LEVEL_LABELS = {RX: "RX", INTER_PLUS: "INTER+", INTER: "INTER"}
"""Display labels for the difficulty levels Strivee publishes, hardest first."""


@dataclass(frozen=True)
class ProgrammingBlock:
    """A named programming block within a day (e.g. 'Back Squat', 'WOD')."""

    name: str
    content: str  # the prescription in play: RX as parsed, the chosen level after selection
    instruction: str = ""  # coach notes / intent, kept separate from the prescription
    inter_plus: str = ""  # INTER+ variant as published, "" when the source has none
    inter: str = ""  # INTER variant as published, "" when the source has none
    level: str = RX  # which level `content` currently holds

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("ProgrammingBlock.name must be a non-empty string")

    def replace(self, **changes: str) -> "ProgrammingBlock":
        """Return a copy of this block with the given fields replaced."""
        return replace(self, **changes)

    def level_text(self, level: str) -> str:
        """Return the prescription the source published for *level*.

        RX resolves to ``content`` because that is where the parser puts the RX
        (or single-level) prescription; selection overwrites it, so call this on
        a freshly-parsed block.
        """
        return {RX: self.content, INTER_PLUS: self.inter_plus, INTER: self.inter}[level]

    def available_levels(self) -> list[str]:
        """Levels this block offers, hardest first — always at least ``[RX]``."""
        return [lv for lv in LEVEL_LABELS if self.level_text(lv).strip()] or [RX]


@dataclass(frozen=True)
class DayProgramming:
    """All programming blocks for a single training day."""

    date: date
    day_label: str  # e.g. "Mon", "Tue"
    blocks: list[ProgrammingBlock] = field(default_factory=list)


@dataclass(frozen=True)
class WeeklyProgramming:
    """A full week of programming, keyed by the Monday start date."""

    week_start: date
    days: list[DayProgramming] = field(default_factory=list)


# ── Cardio (Garmin → BTWB) ────────────────────────────────────────────────────

SINGLE_DISTANCE = "single_distance"
"""BTWB's "Single Distance" model — one continuous effort, scored on total time."""

INTERVALS = "intervals"
"""BTWB's "Intervals / Repeats" model — several efforts of a set distance, each for time.

Not "For Distance", which fixes the *time* per effort and measures how far you
got. A 5 x 500m prescription is the former.
"""


@dataclass(frozen=True)
class CardioInterval:
    """One work effort of an interval session, and the rest that followed it."""

    distance_m: float
    duration_s: float
    rest_s: float | None = None  # None when nothing was recorded after this effort


@dataclass(frozen=True)
class CardioSession:
    """A completed run or ride, in the shape BTWB's logger accepts."""

    date: date
    movement: str  # BTWB movement name, e.g. "Run" / "Road Bike"
    model: str  # SINGLE_DISTANCE | INTERVALS
    title: str
    distance_m: float
    duration_s: float
    intervals: list[CardioInterval] = field(default_factory=list)
    notes: str = ""
    source_ids: tuple[int, ...] = ()
    """Garmin activity IDs folded into this entry — the key the sync dedupes on."""

    def __post_init__(self) -> None:
        if self.model == INTERVALS and len(self.intervals) < 2:
            raise ValueError("An interval session needs at least two work efforts")
