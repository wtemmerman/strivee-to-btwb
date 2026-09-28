"""Unit tests for splitting joined blocks into separate BTWB workouts."""

from strivee_btwb.core.models import INTER, ProgrammingBlock
from strivee_btwb.processing.plus_split import split_plus_joins


def test_each_joined_part_becomes_its_own_workout():
    """Real Wed INTER shape: BTWB mangled the two pieces posted as one workout."""
    block = ProgrammingBlock(
        name="EMF 60 - Strict Handstand push-up",
        content=(
            "WEEK 4 - Development\n3 sets of :\n3 Negative Deficit strict HSPU\n"
            "- Rest 60 sec between sets -\n+\n2 sets of :\nMax rep strict HSPU with Abmat"
        ),
        instruction="Objectif : Accumuler du volume sur le mouvement !",
        level=INTER,
    )
    first, second = split_plus_joins(block)
    assert first.name == "EMF 60 - Strict Handstand push-up (1/2)"
    assert first.content == (
        "WEEK 4 - Development\n3 sets of :\n3 Negative Deficit strict HSPU\n"
        "- Rest 60 sec between sets -"
    )
    assert first.instruction == "Objectif : Accumuler du volume sur le mouvement !"
    assert second.name == "EMF 60 - Strict Handstand push-up (2/2)"
    assert second.content == "2 sets of :\nMax rep strict HSPU with Abmat"
    assert second.instruction == ""  # the note is posted once, not per part
    assert first.level == second.level == INTER


def test_three_parts_are_numbered_out_of_three():
    block = ProgrammingBlock(
        name="EMF 60 : Handstand Walk",
        content=(
            "1 sets of :\nMax time Handstand hold\n+\n2 sets of :\n2 A/R ramp handstand walk\n"
            "+\n3-6 Pirouette handstand walk"
        ),
    )
    assert [b.name for b in split_plus_joins(block)] == [
        "EMF 60 : Handstand Walk (1/3)",
        "EMF 60 : Handstand Walk (2/3)",
        "EMF 60 : Handstand Walk (3/3)",
    ]


def test_into_and_then_join_like_a_plus():
    """Real Energy System Training: the formatter dropped one side of the "Into"."""
    block = ProgrammingBlock(
        name="EMF 60 : Energy System Training",
        content=(
            "6 min Bike erg Warm-up - Increasing pace\n+\n"
            "3 x (20 sec Bike erg #HARD PACE - 40 sec # Recovery Pace)\n\nInto\n\n"
            "For time :\n2000m Row"
        ),
    )
    bike, row = split_plus_joins(block)
    assert bike.content == "3 x (20 sec Bike erg #HARD PACE - 40 sec # Recovery Pace)"
    assert bike.instruction == "6 min Bike erg Warm-up - Increasing pace"
    assert row.content == "For time :\n2000m Row"
    then = ProgrammingBlock(
        name="A", content="Build a new 2RM Strict Pull-up\n\nthen\n\nBuild a new 2RM Strict DIP"
    )
    assert [b.content for b in split_plus_joins(then)] == [
        "Build a new 2RM Strict Pull-up",
        "Build a new 2RM Strict DIP",
    ]


def test_a_join_inside_one_round_does_not_split():
    """Real Sat shape: each set is a hold Into a walk — one workout, not two."""
    block = ProgrammingBlock(
        name="EMF 60 : Handstand Walk",
        content=(
            "4 sets, each for time of :\n\n30 sec wall facing handstand hold\nInto\n"
            "15m Handstand walk as fast as possible\n\n- Rest 1min between sets -"
        ),
    )
    assert split_plus_joins(block) == [block]


def test_a_closed_round_before_the_join_still_splits():
    block = ProgrammingBlock(
        name="A",
        content=(
            "3 sets of :\n5 Deadlift\n- Rest 2min between sets -\n+\n"
            "2 sets of :\nMax HSPU\n- Rest 60 sec between sets -"
        ),
    )
    assert len(split_plus_joins(block)) == 2


def test_run_warm_up_and_cooldown_move_to_the_note():
    """Real Thu run: only the main set is work to log."""
    block = ProgrammingBlock(
        name="EMF RX - Optional EF",
        content=(
            "10min easy warm-up\n+\n8min RPE 4\n4min RPE 7\nx 3sets\n+\n10min walk - cooldown"
        ),
        instruction="Objectif : Run - Endurance / Base aérobie !",
    )
    (run,) = split_plus_joins(block)
    assert run.name == "EMF RX - Optional EF"
    assert run.content == "8min RPE 4\n4min RPE 7\nx 3sets"
    assert run.instruction == (
        "10min easy warm-up\n\nObjectif : Run - Endurance / Base aérobie !\n\n10min walk - cooldown"
    )


def test_bike_ramp_down_is_the_cooldown_even_unnamed():
    """Real Fri bike: the cooldown is written as "#70 to 40%", never as "cooldown"."""
    block = ProgrammingBlock(
        name="EMF 60 - Bike erg",
        content=(
            "5min Warm-up increasing pace to 60% FTP20\n+\n5 sets of :\n2min #55% FTP20\n"
            "2min #65% FTP20\n- no rest between sets -\n+\n5min #70 to 40% FTP20\nTotal - 30min"
        ),
    )
    (bike,) = split_plus_joins(block)
    assert bike.content == (
        "5 sets of :\n2min #55% FTP20\n2min #65% FTP20\n- no rest between sets -"
    )
    assert bike.instruction == (
        "5min Warm-up increasing pace to 60% FTP20\n\n"
        "Cooldown :\n5min #70 to 40% FTP20\nTotal - 30min"
    )


def test_a_rising_range_at_the_end_is_work():
    """ "#70 to 80%" climbs — a last part loading up is not a cooldown."""
    block = ProgrammingBlock(
        name="A", content="3 Power Clean\n+\nEvery 75 sec x 6 sets of :\n1 Snatch #70 to 80%"
    )
    assert len(split_plus_joins(block)) == 2


def test_only_the_ends_can_be_warm_up_or_cooldown():
    """A middle part is work, whatever it mentions."""
    block = ProgrammingBlock(
        name="A",
        content="EMOMx6 :\n3 Power snatch\n+\nWarm-up sets on the bar\n+\nAMRAP 3:00\n5 Burpees",
    )
    assert len(split_plus_joins(block)) == 3


def test_a_block_without_a_join_is_untouched():
    block = ProgrammingBlock(name="A", content="3 sets of :\n5 Back Squat\n+ Rest 2 min")
    assert split_plus_joins(block) == [block]
