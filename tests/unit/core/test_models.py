"""Unit tests for data models."""

import dataclasses
from datetime import date

import pytest

from strivee_btwb.core.models import (
    INTER,
    INTER_PLUS,
    INTERVALS,
    RX,
    CardioInterval,
    CardioSession,
    DayProgramming,
    ProgrammingBlock,
    WeeklyProgramming,
)


def test_block_without_variants_offers_only_rx():
    block = ProgrammingBlock(name="Back Squat", content="5x5 @ 80%")
    assert block.available_levels() == [RX]
    assert block.level == RX


def test_available_levels_lists_published_levels_hardest_first():
    block = ProgrammingBlock(name="WOD", content="20 RMU", inter_plus="10 RMU", inter="20 C2B")
    assert block.available_levels() == [RX, INTER_PLUS, INTER]


def test_available_levels_skips_blank_variants():
    block = ProgrammingBlock(name="WOD", content="20 RMU", inter_plus="   ", inter="20 C2B")
    assert block.available_levels() == [RX, INTER]


def test_level_text_reads_each_published_level():
    block = ProgrammingBlock(name="WOD", content="20 RMU", inter_plus="10 RMU", inter="20 C2B")
    assert block.level_text(RX) == "20 RMU"
    assert block.level_text(INTER_PLUS) == "10 RMU"
    assert block.level_text(INTER) == "20 C2B"


def test_programming_block_fields():
    block = ProgrammingBlock(name="Back Squat", content="5x5 @ 80%")
    assert block.name == "Back Squat"
    assert block.content == "5x5 @ 80%"


def test_programming_block_is_frozen():
    block = ProgrammingBlock(name="WOD", content="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        block.content = "y"


def test_programming_block_rejects_empty_name():
    with pytest.raises(ValueError, match="non-empty"):
        ProgrammingBlock(name="   ", content="x")


def test_programming_block_replace_returns_edited_copy():
    block = ProgrammingBlock(name="WOD", content="raw", instruction="note")
    edited = block.replace(content="clean")
    assert edited.content == "clean"
    assert edited.name == "WOD"
    assert edited.instruction == "note"
    assert block.content == "raw"  # original untouched


def test_day_programming_defaults_empty_blocks():
    day = DayProgramming(date=date(2026, 4, 27), day_label="Mon")
    assert day.blocks == []


def test_day_programming_with_blocks():
    blocks = [
        ProgrammingBlock(name="WOD", content="21-15-9 Thrusters / Pull-ups"),
        ProgrammingBlock(name="Cool-down", content="5 min walk"),
    ]
    day = DayProgramming(date=date(2026, 4, 28), day_label="Tue", blocks=blocks)
    assert len(day.blocks) == 2
    assert day.blocks[0].name == "WOD"


def test_weekly_programming_defaults_empty_days():
    week = WeeklyProgramming(week_start=date(2026, 4, 27))
    assert week.days == []


def test_weekly_programming_with_days():
    days = [
        DayProgramming(date=date(2026, 4, 27), day_label="Mon"),
        DayProgramming(date=date(2026, 4, 28), day_label="Tue"),
    ]
    week = WeeklyProgramming(week_start=date(2026, 4, 27), days=days)
    assert len(week.days) == 2
    assert week.week_start == date(2026, 4, 27)


def test_an_interval_session_needs_more_than_one_effort():
    """One effort is a single distance; calling it intervals would invent a rep scheme."""
    with pytest.raises(ValueError, match="two work efforts"):
        CardioSession(
            date=date(2026, 8, 5),
            movement="Run",
            model=INTERVALS,
            title="5 x 500m",
            distance_m=8990,
            duration_s=3066,
            intervals=[CardioInterval(500, 118)],
        )


def test_every_plan_field_survives_a_json_round_trip():
    """A hand-written cache once dropped percent_1rm and rep_max; asdict cannot."""
    import json

    from strivee_btwb.core.models import ClassicSets, ErgIntervals, plan_from_json, plan_to_json

    sets = ClassicSets(
        "Tempo Back Squat", (5, 5, None), 120, percent_1rm=70, rep_max=True, emom_seconds=75
    )
    erg = ErgIntervals("Bike Erg", (120, 60), 40)
    for cls, plan in ((ClassicSets, sets), (ErgIntervals, erg)):
        assert plan_from_json(cls, json.loads(json.dumps(plan_to_json(plan)))) == plan
    assert plan_to_json(None) is None
    assert plan_from_json(ClassicSets, None) is None


def test_a_plan_written_by_another_version_fails_loud():
    import pytest

    from strivee_btwb.core.models import ClassicSets, plan_from_json

    with pytest.raises(ValueError, match="do not match"):
        plan_from_json(ClassicSets, {"movement": "Seal Row", "reps": [10], "rest_seconds": None})
