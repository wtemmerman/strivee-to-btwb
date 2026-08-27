"""Unit tests for the accessory audit's set-counting rules.

These pin the judgement calls, not the model: every WorkSet here is hand-written,
so a failure means the credit table or the movement lookup changed, never that
the LLM had a bad day.
"""

import json

import pytest

from strivee_btwb.core import config
from strivee_btwb.processing.volume import (
    ACCESSORY,
    GYMNASTICS_MAX,
    HEAVY_SINGLE,
    METCON,
    METCON_CAP,
    SKILL,
    STRENGTH,
    WorkSet,
    _normalise,
    muscles_for,
    pool_muscles,
    weekly_volume,
)


def _volume(muscle: str, *work_sets: WorkSet) -> float:
    volumes, _ = weekly_volume(work_sets)
    return volumes[muscle].credited


# ── Credit by block type ──────────────────────────────────────────────────────


def test_strength_sets_count_in_full():
    assert _volume("chest", WorkSet("Bench Press", sets=4, block_type=STRENGTH)) == 4.0


def test_a_max_effort_gymnastics_set_counts_as_a_full_set():
    assert _volume("biceps", WorkSet("Strict Bar Muscle-up", 1, block_type=GYMNASTICS_MAX)) == 0.6


def test_heavy_singles_count_for_nothing():
    """'Build to a 1RM' is one rep of tension — neural work, not volume."""
    assert _volume("quad_rf", WorkSet("Back Squat", sets=6, block_type=HEAVY_SINGLE)) == 0.0


def test_skill_work_counts_for_nothing():
    assert _volume("chest", WorkSet("Ring Dip", sets=5, block_type=SKILL)) == 0.0


def test_conditioning_counts_a_quarter_of_a_set():
    assert _volume("chest", WorkSet("Push-up", sets=4, block_type=METCON)) == pytest.approx(0.7)


# ── The metcon cap ────────────────────────────────────────────────────────────


def test_conditioning_cannot_exceed_the_weekly_cap():
    """A week of high-rep metcons must not read as 'every muscle covered'."""
    volumes, _ = weekly_volume([WorkSet("Strict Chin-up", sets=200, block_type=METCON)])
    assert volumes["biceps"].credited == METCON_CAP
    assert volumes["biceps"].metcon_raw == pytest.approx(50.0)


def test_the_cap_applies_once_per_week_not_once_per_block():
    blocks = [
        WorkSet("Strict Chin-up", sets=40, block_type=METCON, source=f"b{i}") for i in range(6)
    ]
    volumes, _ = weekly_volume(blocks)
    assert volumes["biceps"].credited == METCON_CAP


def test_the_cap_does_not_limit_real_sets():
    """Only conditioning is capped; dedicated work stacks on top of it."""
    volumes, _ = weekly_volume(
        [
            WorkSet("Strict Chin-up", sets=200, block_type=METCON),
            WorkSet("Dumbbell Curl", sets=3, block_type=ACCESSORY),
        ]
    )
    assert volumes["biceps"].credited == METCON_CAP + 3.0


# ── Movement lookup ───────────────────────────────────────────────────────────


def test_the_more_specific_wording_wins():
    """'Strict Pull-up' contains 'Pull-up' and is worth twice as much."""
    assert muscles_for("Strict Pull-up") == {"biceps": 0.7}
    assert muscles_for("Pull-up") == {"biceps": 0.35}


def test_loads_are_stripped_before_the_alternatives_split():
    assert _normalise("2 Clean and jerk #50/35kg") == "2 clean and jerk"
    assert muscles_for("Clean and jerk #50/35kg") == {}


def test_punctuation_and_case_do_not_matter():
    assert muscles_for("bar muscle up") == muscles_for("Bar Muscle-up")


def test_an_unlisted_movement_is_reported_rather_than_scored_zero():
    """A movement that should have counted is a table gap worth seeing."""
    _, unlisted = weekly_volume([WorkSet("Zercher Carry", sets=3, block_type=STRENGTH)])
    assert unlisted == ["Zercher Carry"]


def test_a_movement_mapped_to_nothing_is_not_reported():
    """Explicit {} means 'checked, trains none of them' — not a gap."""
    _, unlisted = weekly_volume([WorkSet("Echo Bike", sets=6, block_type=METCON)])
    assert unlisted == []


def test_one_movement_can_credit_several_muscles():
    volumes, _ = weekly_volume([WorkSet("Ring Row", sets=4, block_type=STRENGTH)])
    assert volumes["biceps"].credited == pytest.approx(2.0)
    assert volumes["rear_delt"].credited == pytest.approx(1.6)


# ── Reporting ─────────────────────────────────────────────────────────────────


def test_gap_is_the_shortfall_against_the_pool_target():
    volumes, _ = weekly_volume([WorkSet("Bench Press", sets=1, block_type=STRENGTH)])
    chest = volumes["chest"]
    assert (chest.target, chest.credited, chest.gap) == (3.0, 1.0, 2.0)


def test_gap_never_goes_negative_when_a_muscle_is_over_target():
    volumes, _ = weekly_volume([WorkSet("Bench Press", sets=20, block_type=STRENGTH)])
    assert volumes["chest"].gap == 0.0


def test_every_pool_muscle_is_reported_even_with_no_programming():
    volumes, _ = weekly_volume([])
    assert set(volumes) == set(pool_muscles())
    assert all(v.credited == 0.0 for v in volumes.values())


def test_sources_name_what_earned_the_credit_largest_first():
    volumes, _ = weekly_volume(
        [
            WorkSet("Ring Dip", sets=2, block_type=STRENGTH),
            WorkSet("Bench Press", sets=4, block_type=STRENGTH),
        ]
    )
    assert volumes["chest"].sources[0] == ("Bench Press", 4.0)


# ── Data-file invariants ──────────────────────────────────────────────────────


def test_the_two_data_files_agree_on_muscle_keys():
    """A typo'd muscle key would silently credit nothing; muscles_for() raises instead."""
    raw = json.loads((config.DATA_DIR / "movement_muscles.json").read_text())
    credited = {m for weights in raw["movements"].values() for m in weights}
    assert credited <= set(pool_muscles())


def test_every_pool_movement_can_be_performed_somewhere():
    for muscle, spec in pool_muscles().items():
        for movement in spec["movements"]:
            assert movement["locations"], f"{muscle}/{movement['id']} has no location"


def test_a_slash_inside_a_movement_name_is_not_an_alternatives_split():
    """'A/R ramp handstand walk' used to normalise to 'a', losing the movement."""
    assert _normalise("A/R ramp handstand walk") == "a r ramp handstand walk"
    assert muscles_for("2 A/R ramp handstand walk") == {}


def test_a_spaced_slash_still_splits_alternatives():
    assert _normalise("Bar Muscle-up / CTB") == "bar muscle up"
    assert muscles_for("Strict HSPU / Abmat strict HSPU") == {"triceps_long": 0.6, "chest": 0.2}
