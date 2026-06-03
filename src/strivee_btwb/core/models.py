"""Shared data models for the strivee-btwb pipeline.

The models are frozen: every pipeline stage produces a new value rather than
mutating in place (capture → parse → clean → format → post), so immutability
matches how the data actually flows and rules out a stage accidentally editing a
cached object. Use :meth:`ProgrammingBlock.replace` to derive an edited copy.
"""

from dataclasses import dataclass, field, replace
from datetime import date


@dataclass(frozen=True)
class ProgrammingBlock:
    """A named programming block within a day (e.g. 'Back Squat', 'WOD')."""

    name: str
    content: str
    instruction: str = ""  # coach notes / intent, kept separate from the prescription

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("ProgrammingBlock.name must be a non-empty string")

    def replace(self, **changes: str) -> "ProgrammingBlock":
        """Return a copy of this block with the given fields replaced."""
        return replace(self, **changes)


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
