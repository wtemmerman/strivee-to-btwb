"""Shared data models for the strivee-btwb pipeline."""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class ProgrammingBlock:
    """A named programming block within a day (e.g. 'Back Squat', 'WOD')."""

    name: str
    content: str
    instruction: str = ""  # coach notes / intent, kept separate from the prescription


@dataclass
class DayProgramming:
    """All programming blocks for a single training day."""

    date: date
    day_label: str  # e.g. "Mon", "Tue"
    blocks: list[ProgrammingBlock] = field(default_factory=list)


@dataclass
class WeeklyProgramming:
    """A full week of programming, keyed by the Monday start date."""

    week_start: date
    days: list[DayProgramming] = field(default_factory=list)
