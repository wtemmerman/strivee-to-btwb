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


def test_emom_of_one_movement():
    """Real Mon 09-28 INTER part; the title's "(OPTION)" does not stop it agreeing."""
    block = _titled(
        "EMF Rx - Chest to bar pull-up (OPTION)",
        "EMOMx12 :\n6 reps Butterfly Chest to bar pull-up Unbroken",
    )
    plan = classic_sets(block)
    assert plan == ClassicSets("Butterfly Chest-to-bar Pull-up", (6,) * 12, None, emom_seconds=60)
    assert describe_sets(plan) == (
        "Butterfly Chest-to-bar Pull-up - EMOM\n6 reps every 1:00 for 12:00"
    )


def test_tempo_cycle_top_set_and_back_off():
    """Real Mon 09-28: BTWB's AI stored 4x4 for a 1x4 top set and one 1x4 back-off."""
    block = _titled(
        "EMF 60 - Bench press",
        "Week 4/15\n\nTempo Bench Week 4/5\n\nTempo 32X1 - 3 sec descente\n\nTop set -\n"
        "1x4 RPE 9 (Target - 188.5 lb-201 lb)\n\nBack OFF -\n1x4 #88% of your today Top set\n\n"
        "- Rest 2 min between sets -",
    )
    assert classic_sets(block) == ClassicSets("Tempo Bench Press", (4, 4), 120)


def test_several_back_off_lines_and_a_rest_range():
    block = _titled(
        "EMF 60 - Bench press",
        "Tempo Bench Week 2/5\n\nTop set -\n1x4 RPE 7 (Target - 179 lb-183.5 lb)\n\nBack OFF -\n"
        "1x4 #92% of your today Top set\n1x4 #88% of your today Top set\n\n"
        "- Rest 1min30 - 2min between sets -",
    )
    assert classic_sets(block) == ClassicSets("Tempo Bench Press", (4, 4, 4), 90)


def test_lettered_top_set_and_back_off_without_tempo():
    """Real Jul-Aug shape: "A. Top Set / Build a set of N" then "B. Back Off / N Sets of R"."""
    block = _titled(
        "EMF 60 : Back Squat",
        "A. Top Set\nBuild a set of 3 Reps RPE 7\n\nB. Back Off\n"
        "3 Sets of 4 Reps @90% of your today 3 reps\n\n- Rest 2min between sets -\n\n"
        "RPE 7 : 3 répétitions en réserve",
    )
    assert classic_sets(block) == ClassicSets("Back Squat", (3, 4, 4, 4), 120)


def test_top_set_without_back_off_is_left_to_the_ai_path():
    block = _titled("EMF 60 - Bench press", "Top set -\n1x4 RPE 9\n\n- Rest 2 min between sets -")
    assert classic_sets(block) is None


def test_every_seconds_emom_with_cue_and_load_around_it():
    """Real Wed 09-28: posted as "Every 1:15 for 7:30: Squat Snatch", the slow pull lost."""
    block = _titled(
        "EMF 60 - Snatch",
        "Every 75 sec x 6 sets of :\n\n1 Slow Pull Squat Snatch (5 sec floor to hip)\n\n"
        "#70 to 80% 129.5 lb/148 lb\n\nFROM THE GROUND\n\nForce de position et bon placement.",
    )
    plan = classic_sets(block)
    assert plan == ClassicSets("Slow Pull Squat Snatch", (1,) * 6, None, emom_seconds=75)
    assert describe_sets(plan) == "Slow Pull Squat Snatch - EMOM\n1 rep every 1:15 for 7:30"


def test_emom_without_the_word_reps_and_after_a_load_line():
    assert classic_sets(_titled("EMF 60 : Weighted Pull-up", "EMOMx6 :\n2 Weighted Pull-up")) == (
        ClassicSets("Weighted Pull-up", (2,) * 6, None, emom_seconds=60)
    )
    press = _titled("EMF 60 : Push Press", "Push press heavy\n\nEMOMx6 :\n2 Push press")
    assert classic_sets(press) == ClassicSets("Push Press", (2,) * 6, None, emom_seconds=60)


def test_every_minutes_and_seconds_header():
    block = _titled(
        "EMF 60 : Power Clean",
        "Every 90 sec x 6 sets of :\n"
        "3 Touch and Go power clean #90% of your last week heavy 5 reps",
    )
    assert classic_sets(block) == ClassicSets("Power Clean", (3,) * 6, None, emom_seconds=90)


def test_an_emom_with_more_structure_or_a_complex_is_left_alone():
    """Real 06-08: four rounds of EMOMx3 with a rest, not one EMOM of three minutes."""
    rounds = _titled(
        "EMF 60 : Clean and Jerk",
        "EMOMx3\n1 Squat Clean And Jerk\n\n- Rest 1min -\n\nx4 sets\n\nSet 1 - 70% (les 3minutes)",
    )
    assert classic_sets(rounds) is None
    complex_ = _titled(
        "EMF 60 : Overhead squat",
        "Every 1min30 x 4 sets of :\n\n5 Overhead squat from Ground (Clean + back rack + jerk)",
    )
    assert classic_sets(complex_) is None
    start = _titled(
        "EMF 60 : Overhead squat",
        "Every 1min30 x4 sets of :\n4 Overhead squat from Ground (Clean and Jerk Start)",
    )
    assert classic_sets(start) is None
    alternating = _titled(
        "EMF 60 - Snatch",
        "EMOMx6\n\nMin 1 - 1 Pause Power snatch #Above the Knee\nMin 2 -1 Pause Squat snatch",
    )
    assert classic_sets(alternating) is None


def test_an_emom_loaded_off_an_rm_stays_on_the_ai_path():
    """Real 04-13 / 05-18: the % of 1RM or 5RM would be lost from the workout."""
    assert (
        classic_sets(
            _titled(
                "EMF 60 : Strict press", "Strict press\n\nEMOMx5 :\n1 strict press #86% of your 1RM"
            )
        )
        is None
    )
    assert (
        classic_sets(
            _titled("EMF 60 : Push Press", "#85% of your 5RM from week 1\n\nEMOMx6 :\n2 Push press")
        )
        is None
    )
