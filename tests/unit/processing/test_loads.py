"""Unit tests for reading logged loads out of a BTWB result line.

Every result string here was copied from a real logged session.
"""

from strivee_btwb.processing.loads import loads_by_movement, parse_result_loads


def test_per_set_loads_come_back_in_row_order():
    loads = parse_result_loads("1295 lbs | 295 lbs, 250 lbs, and 250 lbs | Rx'd ⚡ 80 lbs")
    assert [str(x) for x in loads] == ["295 lbs", "250 lbs", "250 lbs"]


def test_the_total_volume_field_is_not_taken_as_a_set():
    """1950 lbs is the session total; counting it would report one enormous lift."""
    loads = parse_result_loads("1950 lbs | 130 lbs, 130 lbs, 130 lbs, 130 lbs, and 130 lbs | Rx'd")
    assert len(loads) == 5
    assert all(x.value == 130 for x in loads)


def test_a_pr_marker_after_the_scaling_is_ignored():
    assert len(parse_result_loads("2540 lbs | 205 lbs, 215 lbs, and 215 lbs | Rx'd -80 lbs")) == 3


def test_kilos_and_french_decimals_are_read():
    loads = parse_result_loads("total | 62,5 kg, and 60 kg | Rx'd")
    assert [str(x) for x in loads] == ["62,5 kg", "60 kg"]


def test_a_result_with_no_load_yields_nothing():
    """A for-time or max-rep session records a duration or a count, not a load."""
    assert parse_result_loads("8 mins 40 secs | Rx'd") == []
    assert parse_result_loads("") == []


# ── Reading loads off the logged rows ─────────────────────────────────────────

# Copied from real logged sessions, chrome included.
_BACK_SQUAT = [
    "18/08/2026  6:39 PM",
    "1 Back Squat | 295 lbs",
    "57",
    "2 Back Squats | 250 lbs",
    "45",
    "2 Back Squats | 250 lbs",
    "45",
]
_RESULT = "1295 lbs | 295 lbs, 250 lbs, and 250 lbs | Rx'd ⚡ 80 lbs"


def test_each_row_carries_its_own_load():
    paired = loads_by_movement(_BACK_SQUAT, _RESULT)
    assert [str(x) for x in paired["Back Squat"]] == ["295 lbs", "250 lbs", "250 lbs"]


def test_the_session_date_and_level_scores_are_not_loads():
    """Lining loads up by position against these put every load on the wrong movement."""
    assert list(loads_by_movement(_BACK_SQUAT, _RESULT)) == ["Back Squat"]


def test_plural_and_singular_rows_are_one_movement():
    """BTWB writes '1 Back Squat' and '2 Back Squats' in the same session."""
    assert len(loads_by_movement(_BACK_SQUAT, _RESULT)["Back Squat"]) == 3


def test_several_movements_keep_their_own_loads():
    rows = ["12 Cable Lateral Raise | 10 kg", "15 Pec Deck | 25 kg"]
    paired = loads_by_movement(rows)
    assert [str(x) for x in paired["Cable Lateral Raise"]] == ["10 kg"]
    assert [str(x) for x in paired["Pec Deck"]] == ["25 kg"]


def test_a_row_count_disagreeing_with_the_result_returns_nothing():
    """A missed row would understate what was lifted; say nothing instead."""
    assert loads_by_movement(["1 Back Squat | 295 lbs"], _RESULT) == {}


def test_rows_without_a_load_are_skipped():
    assert loads_by_movement(["50 Strict Pull-ups", "Time cap: 10 mins"]) == {}


def test_no_rows_yields_nothing():
    assert loads_by_movement([]) == {}
