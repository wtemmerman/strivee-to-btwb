"""Unit tests for applying a level published only as a scale line."""

from strivee_btwb.processing.scale import apply_scale

MIXED_MODAL_10_26 = (
    "Every 5min x 3 sets of :\n\n50m Farmer Carry #2x22,5/2x15kg\n"
    "10 Db Devil press #2x22,5/2x15kg\n50m Farmer Carry #2x22,5/2x15kg\n10-6 Bar Muscle-up"
)


def test_the_chain_first_step_replaces_the_rx_line():
    """Real Wed 10-28: the INTER level was only its scale line and was rewritten by hand."""
    scaled = apply_scale(
        MIXED_MODAL_10_26,
        "Scale - 6 Bar muscle-up -> 3 Bar Muscle-up -> 8 Burpees bar muscle-up",
    )
    assert scaled == MIXED_MODAL_10_26.replace("10-6 Bar Muscle-up", "3 Bar Muscle-up")


def test_a_level_with_more_than_scale_lines_is_left_as_published():
    """Real Wed 11-04: the INTER is written out; its scale is a further option."""
    assert (
        apply_scale(
            "For time :\n250/200m Row\n5 Rope climb",
            "200/150m Row - 3 Rope climb\nScale rope climb -> 6 Lying to standing rope pull",
        )
        is None
    )


def test_a_scale_whose_movement_is_not_on_one_rx_line_is_left_as_published():
    assert apply_scale(MIXED_MODAL_10_26, "Scale - 6 Ring Muscle-up -> 3 Ring Muscle-up") is None
    assert (
        apply_scale(
            "4 Bar Muscle-up\n10 Box Jump\n4 Bar Muscle-up",
            "Scale - 4 Bar muscle-up -> 2 Bar Muscle-up",
        )
        is None
    )
