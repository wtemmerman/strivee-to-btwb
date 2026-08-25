"""Unit tests for reading a block's sets out of the model's answer.

The model is mocked throughout: what is under test is the validation that stands
between a sloppy response and the audit's arithmetic.
"""

import json
from unittest.mock import patch

from strivee_btwb.core.models import ProgrammingBlock
from strivee_btwb.processing.set_extract import extract_sets

_BLOCK = ProgrammingBlock(name="EMF 60 : Bench press", content="5RM Barbell bench press")


def _respond(payload: str):
    return patch("strivee_btwb.processing.set_extract.chat_json", return_value=payload)


def _sets(*items: dict) -> str:
    return json.dumps({"sets": list(items)})


def test_a_well_formed_response_becomes_work_sets():
    payload = _sets({"movement": "Bench Press", "sets": 1, "reps": "5", "block_type": "strength"})
    with _respond(payload):
        result = extract_sets(_BLOCK)
    assert len(result) == 1
    got = (result[0].movement, result[0].sets, result[0].reps, result[0].block_type)
    assert got == ("Bench Press", 1, "5", "strength")


def test_the_block_name_is_carried_so_the_report_can_cite_it():
    with _respond(_sets({"movement": "Bench Press", "sets": 1, "block_type": "strength"})):
        assert extract_sets(_BLOCK)[0].source == "EMF 60 : Bench press"


def test_an_unknown_block_type_is_dropped_rather_than_counted():
    """An invalid type would raise inside weekly_volume; stop it at the boundary."""
    with _respond(_sets({"movement": "Bench Press", "sets": 1, "block_type": "hypertrophy"})):
        assert extract_sets(_BLOCK) == []


def test_entries_without_a_movement_are_dropped():
    with _respond(_sets({"movement": "  ", "sets": 3, "block_type": "strength"})):
        assert extract_sets(_BLOCK) == []


def test_zero_and_negative_set_counts_are_dropped():
    with _respond(
        _sets(
            {"movement": "Bench Press", "sets": 0, "block_type": "strength"},
            {"movement": "Ring Dip", "sets": -2, "block_type": "strength"},
        )
    ):
        assert extract_sets(_BLOCK) == []


def test_a_valid_entry_survives_alongside_an_invalid_one():
    with _respond(
        _sets(
            {"movement": "Bench Press", "sets": 1, "block_type": "strength"},
            {"movement": "Ring Dip", "sets": 2, "block_type": "nonsense"},
        )
    ):
        result = extract_sets(_BLOCK)
    assert [r.movement for r in result] == ["Bench Press"]


def test_a_truncated_response_is_repaired_rather_than_lost():
    with _respond('{"sets": [{"movement": "Bench Press", "sets": 1, "block_type": "strength"}'):
        assert len(extract_sets(_BLOCK)) == 1


def test_an_empty_response_counts_as_no_sets():
    with _respond("   "):
        assert extract_sets(_BLOCK) == []


def test_a_response_of_the_wrong_shape_counts_as_no_sets():
    with _respond(json.dumps({"blocks": []})):
        assert extract_sets(_BLOCK) == []


def test_a_block_with_no_movements_is_allowed_to_be_empty():
    with _respond(_sets()):
        assert extract_sets(_BLOCK) == []
