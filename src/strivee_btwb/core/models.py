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
