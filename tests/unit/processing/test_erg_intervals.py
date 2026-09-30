"""Unit tests for reading a cardio main set as BTWB "Intervals For Distance"."""

from strivee_btwb.core.models import ErgIntervals, ProgrammingBlock
from strivee_btwb.processing.erg_intervals import describe, erg_intervals


def _block(name: str, content: str, instruction: str = "") -> ProgrammingBlock:
    return ProgrammingBlock(name=name, content=content, instruction=instruction)


def test_one_piece_per_set_with_its_rest():
    """Real Tue 09-28: BTWB asked for a distance, then refused the block."""
    block = _block(
        "EMF 60 - Bike erg",
        "Week 4 - PMA\n\nWarm-up\n\n6min Bike erg #Increasing Pace easy to FTP20 pace\n"
        "1min #FTP20 Pace / 1min easy\n\n- Rest 1Min and GO -\n\n4 sets of :\n"
        "2min #103-108% of FTP20 (290 W-305 W)\n2min Full REST\n\nTotal : 25min\n\nRPE 8",
    )
    assert erg_intervals(block) == ErgIntervals("Bike Erg", (120,) * 4, 120)


def test_pieces_without_rest_repeat_in_order():
    block = _block(
        "EMF RX - Bike erg",
        "2 sets of :\n\n3min #55% FTP20\n2min #60% FTP20\n2min #65% FTP20\n3min #60% FTP20\n\n"
        "- no rest between sets -",
    )
    plan = erg_intervals(block)
    assert plan is not None
    assert plan.intervals == (180, 120, 120, 180) * 2
    assert plan.rest_seconds == 0


def test_trailing_set_count_and_movement_from_the_note():
    """Real Thu run: the title says "EF", only the note says it is a run."""
    block = _block(
        "EMF RX - Optional EF",
        "8min RPE 4\n4min RPE 7\nx 3sets",
        "Objectif : Run - Endurance / Base aérobie !",
    )
    assert erg_intervals(block) == ErgIntervals("Run", (480, 240) * 3, 0)


def test_rest_between_sets_for_a_single_piece():
    block = _block("Row", "5 sets of :\n3min Row #Last week 2K pace\n- Rest 1min between sets -")
    assert erg_intervals(block) == ErgIntervals("Row", (180,) * 5, 60)


def test_rest_between_sets_of_several_pieces_is_declined():
    """BTWB holds one rest for all intervals — it cannot rest only between sets."""
    block = _block(
        "Bike erg",
        "3 sets of :\n3min #90% FTP20\n2min #70% FTP20\n- Rest 2min between sets -",
    )
    assert erg_intervals(block) is None


def test_a_gymnastics_set_is_not_an_erg_set_whatever_the_block_mentions():
    """Real Wed HSPU: active rest on the ski erg, but the work is handstand push-ups."""
    block = _block(
        "EMF 60 - Strict Handstand push-up",
        "3 sets of :\n1min Max rep Strict HSPU Unbroken\n"
        "- Active rest on ski erg #Recovery pace for 60sec -",
    )
    assert erg_intervals(block) is None


def test_two_modalities_are_declined():
    block = _block(
        "EMF 60 : Energy System Training",
        "2 sets of :\n2min Bike erg #HARD\n2min Row #Recovery",
    )
    assert erg_intervals(block) is None


def test_a_steady_effort_after_its_warm_up_is_one_interval():
    """Real Fri 09-07: a warm-up line, one 40-minute effort, a total."""
    block = _block(
        "EMF 60 - Bike erg",
        "Bike Erg : Endurance Fondamentale\n\n5min Warm-up increasing pace to 60% FTP20\n\n"
        "40min Steady State #55-60% FTP 20\n\nTotal - 45min",
    )
    assert erg_intervals(block) == ErgIntervals("Bike Erg", (2400,), 0)


def test_a_test_after_a_warm_up_section_is_one_interval():
    """Real Tue 09-07: the warm-up section's own timed lines are not the effort."""
    block = _block(
        "EMF 60 - Bike erg",
        "Warm-up\n\n6min Bike erg #Increasing Pace\n1min #Target Pace / 1min easy\n\n"
        "- Rest 1Min and GO -\n\nTEST -\n20min Max wattage Bike erg",
    )
    assert erg_intervals(block) == ErgIntervals("Bike Erg", (1200,), 0)


def test_a_free_easy_session_under_a_header():
    block = _block(
        "EMF RX - Optional EF",
        "Endurance FOCUS - 60min\n\n60min easy run - free session (MAIS ça doit rester easy !)",
    )
    assert erg_intervals(block) == ErgIntervals("Run", (3600,), 0)


def test_a_range_or_a_distance_is_left_to_the_ai_path():
    assert (
        erg_intervals(_block("EMF 60 - Optional RUN", "Easy long run -\n\n20-40 min #easy RPE 3-4"))
        is None
    )
    assert (
        erg_intervals(
            _block(
                "EMF 60 : Run Session", "Warm-up\n400m easy run\n\nTEST\n1 miles (1600m) For Time"
            )
        )
        is None
    )


def test_describe_a_single_effort():
    assert describe(ErgIntervals("Run", (3600,), 0)) == "Run - Intervals For Distance\n1 x 60:00"


def test_describe_says_what_will_be_posted():
    assert describe(ErgIntervals("Bike Erg", (120,) * 4, 120)) == (
        "Bike Erg - Intervals For Distance\n4 x 2:00, rest 2:00"
    )
    assert describe(ErgIntervals("Run", (480, 240), 0)) == (
        "Run - Intervals For Distance\n2 intervals: 8:00, 4:00, no rest"
    )
