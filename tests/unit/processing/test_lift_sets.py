"""Unit tests for reading a single-movement set scheme for BTWB's classic Sets template."""

from strivee_btwb.core.models import ClassicSets, ProgrammingBlock
from strivee_btwb.processing.lift_sets import btwb_movement_name, classic_sets, describe_sets


def _block(content: str) -> ProgrammingBlock:
    return ProgrammingBlock(name="EMF 60 - Strict Handstand push-up (2/2)", content=content)


def test_max_rep_sets_with_rest():
    """Real Wed 09-30 part: BTWB's AI returned no preview for it, however often asked."""
    block = _block("2 sets of :\nMax rep strict HSPU with Abmat\n- Rest 75 sec -")
    assert classic_sets(block) == ClassicSets("Strict Handstand Push-up", (None, None), 75)


def test_rest_between_sets_in_minutes():
    block = _block("3 sets of :\nMax rep strict HSPU with Abmat\n- Rest 2min between sets -")
    assert classic_sets(block) == ClassicSets("Strict Handstand Push-up", (None,) * 3, 120)


def test_no_rest_line_is_rest_as_needed():
    plan = classic_sets(_block("3 sets of :\nMax reps Strict Pull-up"))
    assert plan is not None
    assert plan.rest_seconds is None


def test_anything_beyond_the_scheme_stays_on_the_ai_path():
    """Real 09-14 part: an active rest on the ski erg is not a rest BTWB's field can hold."""
    block = _block(
        "3 sets of :\nMax rep Strict HSPU Unbroken with 2RIR\n"
        "- Active rest on ski erg #Recovery pace for 60sec -"
    )
    assert classic_sets(block) is None
    assert classic_sets(_block("3 sets of :\n3 Negative Deficit strict HSPU")) is None


def test_movement_names_drop_the_aid_and_expand_abbreviations():
    assert btwb_movement_name("strict HSPU with Abmat") == "Strict Handstand Push-up"
    assert btwb_movement_name("butterfly C2B") == "Butterfly Chest-to-bar Pull-up"
    # Real corpus lines: a coaching qualifier, a shouted word, spelled-out hyphens.
    assert btwb_movement_name("RING muscle-up Unbroken") == "Ring Muscle-up"
    assert btwb_movement_name("Unbroken Chest to bar pull-up") == "Chest-to-bar Pull-up"


def test_describe_sets():
    assert describe_sets(ClassicSets("Strict Handstand Push-up", (None, None), 75)) == (
        "Strict Handstand Push-up - Sets\n2 sets: max, max reps, rest 1:15"
    )
    assert describe_sets(ClassicSets("Strict Pull-up", (5, 5), None)) == (
        "Strict Pull-up - Sets\n2 sets: 5, 5 reps, rest as needed"
    )
