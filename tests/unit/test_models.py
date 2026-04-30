"""Unit tests for data models."""

from datetime import date

from strivee_btwb.core.models import DayProgramming, ProgrammingBlock, WeeklyProgramming


def test_programming_block_fields():
    block = ProgrammingBlock(name="Back Squat", content="5x5 @ 80%")
    assert block.name == "Back Squat"
    assert block.content == "5x5 @ 80%"


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
