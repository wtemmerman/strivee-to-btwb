"""Unit tests for turning the audit's gap into the block BTWB receives."""

from strivee_btwb.processing.accessory import (
    ACCESSORY_BLOCK_NAME,
    build_block,
    plan_accessory,
)
from strivee_btwb.processing.volume import MuscleVolume


def _short(muscle: str, label: str, target: float, credited: float = 0.0) -> MuscleVolume:
    return MuscleVolume(
        muscle=muscle,
        label=label,
        target=target,
        direct=credited,
        metcon=0.0,
        metcon_raw=0.0,
        sources=(),
    )


def _sets_by_day(plan) -> dict[str, int]:
    return {day: sum(p.sets for p in entries) for day, entries in plan.items()}


# ── Choosing movements ────────────────────────────────────────────────────────


def test_a_small_gap_is_closed_with_one_movement():
    plan = plan_accessory({"rear_delt": _short("rear_delt", "Rear delts", 3.0)}, "gym", ["Tue"])
    assert len(plan["Tue"]) == 1
    assert plan["Tue"][0].sets == 3


def test_a_large_gap_is_split_across_two_movements():
    """Six sets of one calf raise skips the soleus; the pool pairs the two on purpose."""
    plan = plan_accessory({"calf": _short("calf", "Calves", 6.0)}, "gym", ["Tue"])
    names = [p.btwb_name for p in plan["Tue"]]
    assert names == ["Standing Calf Raise", "Seated Calf Raise"]
    assert [p.sets for p in plan["Tue"]] == [3, 3]


def test_an_odd_split_puts_the_extra_set_on_the_first_movement():
    plan = plan_accessory({"side_delt": _short("side_delt", "Side delts", 5.0)}, "gym", ["Tue"])
    assert [p.sets for p in plan["Tue"]] == [3, 2]


def test_a_partial_gap_rounds_up_to_a_whole_set():
    """You cannot do 2.1 sets, and rounding down would leave the gap open."""
    plan = plan_accessory(
        {"hamstring": _short("hamstring", "Hamstrings", 3.0, credited=0.9)}, "gym", ["Tue"]
    )
    assert sum(p.sets for p in plan["Tue"]) == 3


def test_movements_sharing_a_btwb_name_are_only_offered_once():
    """The pool lists the cable lateral raise twice, set up two ways."""
    plan = plan_accessory({"side_delt": _short("side_delt", "Side delts", 5.0)}, "gym", ["Tue"])
    names = [p.btwb_name for p in plan["Tue"]]
    assert len(names) == len(set(names))


def test_a_muscle_with_no_option_at_that_location_is_skipped_loudly(caplog):
    plan = plan_accessory({"quad_rf": _short("quad_rf", "Quads", 3.0)}, "moon", ["Tue"])
    assert plan["Tue"] == []
    assert "No moon option in the pool" in caplog.text


# ── Spreading across days ─────────────────────────────────────────────────────


def test_muscles_already_at_target_are_not_planned():
    volumes = {
        "chest": _short("chest", "Chest", 3.0, credited=6.0),
        "calf": _short("calf", "Calves", 6.0),
    }
    plan = plan_accessory(volumes, "gym", ["Tue"])
    assert {p.muscle for p in plan["Tue"]} == {"calf"}


def test_work_is_spread_across_the_requested_days():
    volumes = {
        "calf": _short("calf", "Calves", 6.0),
        "side_delt": _short("side_delt", "Side delts", 5.0),
        "rear_delt": _short("rear_delt", "Rear delts", 3.0),
    }
    loads = _sets_by_day(plan_accessory(volumes, "gym", ["Tue", "Fri"]))
    assert sum(loads.values()) == 14
    assert min(loads.values()) >= 5  # neither day is left empty


def test_a_muscle_is_never_split_across_two_days():
    """Five lateral raise sets in one session beat two on Tuesday and three on Friday."""
    volumes = {
        "calf": _short("calf", "Calves", 6.0),
        "side_delt": _short("side_delt", "Side delts", 5.0),
    }
    plan = plan_accessory(volumes, "gym", ["Tue", "Fri"])
    for muscle in ("calf", "side_delt"):
        days_used = [d for d, entries in plan.items() if any(p.muscle == muscle for p in entries)]
        assert len(days_used) == 1


def test_one_day_takes_everything():
    volumes = {
        "calf": _short("calf", "Calves", 6.0),
        "side_delt": _short("side_delt", "Side delts", 5.0),
    }
    assert _sets_by_day(plan_accessory(volumes, "gym", ["Wed"])) == {"Wed": 11}


def test_the_same_week_always_plans_the_same_way():
    volumes = {
        "calf": _short("calf", "Calves", 6.0),
        "side_delt": _short("side_delt", "Side delts", 5.0),
        "quad_rf": _short("quad_rf", "Quads", 3.0),
    }
    first = plan_accessory(volumes, "gym", ["Tue", "Fri"])
    assert first == plan_accessory(volumes, "gym", ["Tue", "Fri"])


# ── The block itself ──────────────────────────────────────────────────────────


def test_the_block_title_is_constant():
    """BTWB dedupes on the title, so a title that moved with the gap would double-post."""
    a = build_block(plan_accessory({"calf": _short("calf", "Calves", 6.0)}, "gym", ["Tue"])["Tue"])
    b = build_block(
        plan_accessory({"side_delt": _short("side_delt", "Side delts", 5.0)}, "gym", ["Tue"])["Tue"]
    )
    assert a.name == b.name == ACCESSORY_BLOCK_NAME


def test_the_content_is_one_line_per_movement_in_btwb_wording():
    plan = plan_accessory({"calf": _short("calf", "Calves", 6.0)}, "gym", ["Tue"])
    assert build_block(plan["Tue"]).content == (
        "3 sets of 12-20 Standing Calf Raise\n3 sets of 15-20 Seated Calf Raise"
    )


def test_the_coaching_note_names_the_targets_and_the_effort():
    plan = plan_accessory({"calf": _short("calf", "Calves", 6.0)}, "gym", ["Tue"])
    instruction = build_block(plan["Tue"]).instruction
    assert "Calves" in instruction
    assert "0-2 reps in reserve" in instruction
