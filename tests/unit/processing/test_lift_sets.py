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
    plan = classic_sets(_block("3 sets of :\nMax reps strict HSPU"))
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


def _titled(name: str, content: str) -> ProgrammingBlock:
    return ProgrammingBlock(name=name, content=content)


def test_weighted_sets_with_tempo_and_percent_of_1rm():
    """Real Sat 09-28: BTWB's AI stored "Back Squat 5-5-5" — no tempo, no 70%."""
    block = _titled(
        "EMF 60 - Back Squat",
        "3 sets of :\n5 Reps Back Squat Tempo 31X1\n- Rest 2 min between sets -\n\n"
        "Target weight : #70% 1RM 255.5 lb",
    )
    assert classic_sets(block) == ClassicSets("Tempo Back Squat", (5, 5, 5), 120, percent_1rm=70)


def test_a_reps_line_without_a_movement_takes_the_title_and_drops_the_rpe():
    block = _titled(
        "EMF 60 : Bench press", "3 Sets of :\n\n6 Reps RPE 7\n\n- Rest 2min between sets -"
    )
    assert classic_sets(block) == ClassicSets("Bench Press", (6, 6, 6), 120)


def test_rep_max_after_a_time_window():
    """Real Mon 09-28: BTWB's AI stored a "Burpee Alternating Dumbbell Clean&Jerk"."""
    block = _titled(
        "EMF 60 - Barbell Seal Row",
        "In a 8min window\n\n10RM Barbell Seal Row\n\nRenforcement du pattern horizontal !",
    )
    assert classic_sets(block) == ClassicSets("Seal Row", (10,), None, rep_max=True)


def test_build_a_heavy_double_of_a_paused_lift():
    """Real Sat 09-28: BTWB's AI refused the block outright."""
    block = _titled(
        "EMF 60 - Clean",
        "In a 10min window :\n\nBuild a heavy double - 2-pause Squat clean #Bellow and above "
        "the knee\n\n1 Temps de pause pour chacune des pauses !",
    )
    assert classic_sets(block) == ClassicSets("Pause Squat Clean", (2,), None, rep_max=True)


def test_touch_and_go_is_a_cue_not_part_of_the_name():
    block = _titled("EMF 60 : Power Clean", "5RM Power clean Touch and go")
    assert classic_sets(block) == ClassicSets("Power Clean", (5,), None, rep_max=True)


def test_a_read_that_disagrees_with_the_title_is_left_to_the_ai_path():
    """Real 04-27: a programme header, "3RM en 4 semaines", is not a 3RM of anything."""
    header = _titled(
        "Back Squat", "3RM en 4 semaines\nSemaine 2/4\nTop set:\nBuild to a heavy 3 reps Back Squat"
    )
    assert classic_sets(header) is None
    snatch = _titled(
        "EMF 60 : Snatch under fatigue", "Build a heavy single for the day Squat Snatch"
    )
    assert classic_sets(snatch) is None


def test_describe_rep_max_and_percent():
    assert describe_sets(ClassicSets("Seal Row", (10,), None, rep_max=True)) == (
        "Seal Row - X Rep Max\n10 rep max"
    )
    assert describe_sets(ClassicSets("Tempo Back Squat", (5, 5, 5), 120, percent_1rm=70)) == (
        "Tempo Back Squat - Sets\n3 sets: 5, 5, 5 reps @ 70% 1RM, rest 2:00"
    )


def test_a_load_on_the_reps_line_stays_on_the_ai_path():
    """Real 08-31: read as heaviest weight, the workout lost its 75-80% of 1RM."""
    block = _titled(
        "EMF 60 : Strict press",
        "3 sets of :\n5 Reps Strict press @75-80% of your 1RM\n- Rest 2min between sets -",
    )
    assert classic_sets(block) is None
