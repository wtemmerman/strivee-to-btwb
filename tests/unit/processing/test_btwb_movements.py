"""Unit tests for the Strivee → BTWB movement vocabulary."""

from strivee_btwb.processing.btwb_movements import apply_movement_aliases


def test_single_arm_handstand_hold_becomes_weight_shift():
    text = "Single arm Wall facing handstand Hold x15 sec / arm"
    assert apply_movement_aliases(text) == "Wall Facing Handstand Hold Weight Shift x15 sec / arm"


def test_plain_wall_facing_handstand_hold_is_left_alone():
    """BTWB already knows this one — rewriting it would point at the wrong drill."""
    text = "Wall facing handstand Hold x 30 sec"
    assert apply_movement_aliases(text) == text


def test_both_eccentric_wordings_become_negative_ring_muscle_up():
    assert apply_movement_aliases("12 Negative strict Ring Muscle-up") == (
        "12 Negative Ring Muscle-up"
    )
    assert apply_movement_aliases("min 7 to 9 - 1 Eccentric strict RMU") == (
        "min 7 to 9 - 1 Negative Ring Muscle-up"
    )


def test_chest_to_ring_keeps_its_weighted_qualifier():
    """BTWB expresses the load as an attribute, so 'Weighted' stays around the name."""
    text = "3 Weighted strict chest to ring @50% of your last week heavy 3"
    assert apply_movement_aliases(text) == (
        "3 Weighted Strict Chest-to-Ring Pull-up @50% of your last week heavy 3"
    )


def test_unknown_movements_pass_through_untouched():
    text = "20/16 Ring Muscle-up\nTime cap 6:00"
    assert apply_movement_aliases(text) == text
