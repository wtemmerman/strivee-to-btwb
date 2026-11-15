"""Unit tests for applying a level published only as changes to RX."""

from strivee_btwb.processing.scale import apply_to_rx

MIXED_MODAL_10_26 = (
    "Every 5min x 3 sets of :\n\n50m Farmer Carry #2x22,5/2x15kg\n"
    "10 Db Devil press #2x22,5/2x15kg\n50m Farmer Carry #2x22,5/2x15kg\n10-6 Bar Muscle-up"
)


def test_the_chain_first_step_replaces_the_rx_line():
    """Real Wed 10-28: the INTER level was only its scale line and was rewritten by hand."""
    scaled = apply_to_rx(
        MIXED_MODAL_10_26,
        "Scale - 6 Bar muscle-up -> 3 Bar Muscle-up -> 8 Burpees bar muscle-up",
    )
    assert scaled == MIXED_MODAL_10_26.replace("10-6 Bar Muscle-up", "3 Bar Muscle-up")


def test_a_level_with_more_than_scale_lines_is_left_as_published():
    """Real Wed 11-04: the INTER is written out; its scale is a further option."""
    assert (
        apply_to_rx(
            "For time :\n250/200m Row\n5 Rope climb",
            "200/150m Row - 3 Rope climb\nScale rope climb -> 6 Lying to standing rope pull",
        )
        is None
    )


def test_a_scale_whose_movement_is_not_on_one_rx_line_is_left_as_published():
    assert apply_to_rx(MIXED_MODAL_10_26, "Scale - 6 Ring Muscle-up -> 3 Ring Muscle-up") is None
    assert (
        apply_to_rx(
            "4 Bar Muscle-up\n10 Box Jump\n4 Bar Muscle-up",
            "Scale - 4 Bar muscle-up -> 2 Bar Muscle-up",
        )
        is None
    )


HALF_JACKIE = (
    '"HALF JACKIE" - CrossFit Benchmark\n\nFor time :\n500/400m Row\n25 Thruster #20/15kg\n'
    "15 Pull-up\n\nTime cap : 7min\n\nRX - Target : Sub 4:00"
)


def test_a_one_line_level_swaps_each_piece_into_its_rx_line():
    """Real Wed 11-11: the INTER was one " - "-joined line and was written out by hand."""
    scaled = apply_to_rx(
        HALF_JACKIE, "400/300m Row - 20 Thruster #15/10kg - 10 Pull-up (Banded or Jumping)"
    )
    assert scaled == (
        '"HALF JACKIE" - CrossFit Benchmark\n\nFor time :\n400/300m Row\n20 Thruster #15/10kg\n'
        "10 Pull-up (Banded or Jumping)\n\nTime cap : 7min\n"
    )


def test_same_as_rx_keeps_the_lines_the_level_does_not_name():
    scaled = apply_to_rx(HALF_JACKIE, "Same as RX - 25 Thruster #15/10kg - 15 Pull-up")
    assert scaled is not None
    assert "500/400m Row\n25 Thruster #15/10kg\n15 Pull-up" in scaled


def test_a_one_line_level_that_is_not_only_counted_rx_movements_is_left_as_published():
    assert apply_to_rx(HALF_JACKIE, "400/300m Row - 20 Wall ball") is None
    assert apply_to_rx(HALF_JACKIE, "Same as RX - lighter thrusters") is None
    assert apply_to_rx(HALF_JACKIE, "20 Thruster #15/10kg") is None


SHORT_WOD_11_17 = (
    "AMRAP 8min :\n12/9 Calories Bike erg\n10 KB Swing #24/16kg\n8 Toes to bar\n\n"
    "RX - Target : 4+ rounds"
)


def test_a_load_alone_changes_the_load_of_the_line_it_names():
    """Real Tue 11-17: INTER+ was "Same as RX - KB #20/12kg" and was written out by hand."""
    assert apply_to_rx(SHORT_WOD_11_17, "Same as RX - KB #20/12kg") == (
        "AMRAP 8min :\n12/9 Calories Bike erg\n10 KB Swing #20/12kg\n8 Toes to bar\n"
    )


def test_a_shortened_name_and_a_new_movement_in_place_are_swapped_in():
    """Real Tue 11-17: INTER shortens "Calories Bike erg" and swaps toes to bar by position."""
    assert apply_to_rx(
        SHORT_WOD_11_17, "10/7 Cal Bike - 10 KB Swing #16/12kg - 8 Hanging knee raise"
    ) == ("AMRAP 8min :\n10/7 Cal Bike erg\n10 KB Swing #16/12kg\n8 Hanging knee raise\n")


def test_a_new_movement_needs_every_piece_in_its_rx_place():
    assert apply_to_rx(SHORT_WOD_11_17, "8 Hanging knee raise - 10 KB Swing #16/12kg") is None
    assert (
        apply_to_rx(SHORT_WOD_11_17, "10 KB Swing - 10/7 Cal Bike - 8 Hanging knee raise") is None
    )
    assert apply_to_rx(SHORT_WOD_11_17, "Same as RX - 8 Hanging knee raise") is None


def test_a_chain_to_another_movement_replaces_the_line_outright():
    assert apply_to_rx("For time :\n5 Rope climb", "Scale - 5 Rope climb -> 10 Rope pull") == (
        "For time :\n10 Rope pull"
    )


def test_a_sets_header_is_not_a_movement():
    assert apply_to_rx(
        "3 sets of :\n30 Double under\n8 Chest to bar pull-up",
        "20 Double under - 3 Butterfly Chest to bar pull-up",
    ) == ("3 sets of :\n20 Double under\n3 Butterfly Chest to bar pull-up")
