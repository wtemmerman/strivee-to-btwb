"""Unit tests for reading a movement-per-line block before entering it on BTWB."""

import pytest

from strivee_btwb.btwb.client import _parse_movement_lines
from strivee_btwb.btwb.common import BTWBError
from strivee_btwb.core.models import ProgrammingBlock


def _block(content: str) -> ProgrammingBlock:
    return ProgrammingBlock(name="Accessory", content=content)


def test_each_line_becomes_a_movement_and_its_reps():
    parsed = _parse_movement_lines(_block("12 Cable Lateral Raise\n15 Seated Calf Raise"))
    assert parsed == [("Cable Lateral Raise", "12"), ("Seated Calf Raise", "15")]


def test_repeated_lines_stay_separate_sets():
    """Three rows is the point — one load field each."""
    parsed = _parse_movement_lines(_block("12 Pec Deck\n12 Pec Deck\n12 Pec Deck"))
    assert parsed == [("Pec Deck", "12")] * 3


def test_blank_lines_are_ignored():
    assert len(_parse_movement_lines(_block("12 Pec Deck\n\n  \n10 Cable Curl"))) == 2


def test_a_movement_name_may_contain_digits():
    parsed = _parse_movement_lines(_block("12 Single Arm Banded Lateral Raise"))
    assert parsed == [("Single Arm Banded Lateral Raise", "12")]


def test_an_unreadable_line_raises_rather_than_being_skipped():
    """A silently dropped set is the failure this path exists to remove."""
    with pytest.raises(BTWBError, match="Cannot parse"):
        _parse_movement_lines(_block("12 Pec Deck\n3 rounds for quality:"))


def test_a_line_with_no_rep_count_raises():
    with pytest.raises(BTWBError, match="Cannot parse"):
        _parse_movement_lines(_block("Cable Lateral Raise"))


def test_an_empty_block_raises():
    with pytest.raises(BTWBError, match="no movement lines"):
        _parse_movement_lines(_block("   \n\n"))


# ---------------------------------------------------------------------------
# _build_from_movements — search once, copy the repeats
# ---------------------------------------------------------------------------


def _record_entry(monkeypatch) -> list[tuple[str, str, str]]:
    """Capture how each set is entered, without driving a browser."""
    from strivee_btwb.btwb import client

    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        client, "_add_movement", lambda _p, name, reps: calls.append(("search", name, reps))
    )
    monkeypatch.setattr(
        client, "_copy_movement_row", lambda _p, name, reps: calls.append(("copy", name, reps))
    )
    monkeypatch.setattr(client, "_remove_seed_movement", lambda _p: None)
    return calls


def test_a_repeated_set_is_copied_rather_than_searched_again(monkeypatch):
    """The movement search is the step that stalls; a repeat does not need it."""
    from strivee_btwb.btwb import client

    calls = _record_entry(monkeypatch)
    client._build_from_movements(None, _block("12 Pec Deck\n12 Pec Deck\n12 Pec Deck"))

    assert calls == [
        ("search", "Pec Deck", "12"),
        ("copy", "Pec Deck", "12"),
        ("copy", "Pec Deck", "12"),
    ]


def test_a_repeat_after_another_movement_is_searched_again(monkeypatch):
    """COPIER duplicates the *last* row, so a non-adjacent repeat must not use it."""
    from strivee_btwb.btwb import client

    calls = _record_entry(monkeypatch)
    client._build_from_movements(None, _block("12 Pec Deck\n10 Cable Curl\n12 Pec Deck"))

    assert [c[0] for c in calls] == ["search", "search", "search"]


def test_the_same_movement_at_different_reps_is_searched_again(monkeypatch):
    """A copy carries its source's reps, so a different rep count is a new row."""
    from strivee_btwb.btwb import client

    calls = _record_entry(monkeypatch)
    client._build_from_movements(None, _block("12 Pec Deck\n15 Pec Deck"))

    assert [c[0] for c in calls] == ["search", "search"]


def test_every_set_still_reaches_the_workout(monkeypatch):
    """Copying must not lose a set: 26 lines in, 26 rows entered."""
    from strivee_btwb.btwb import client

    calls = _record_entry(monkeypatch)
    content = "\n".join(["12 Standing Calf Raise"] * 3 + ["10 Cable Curl"] * 2)
    client._build_from_movements(None, _block(content))

    assert len(calls) == 5
    assert [c[1] for c in calls] == ["Standing Calf Raise"] * 3 + ["Cable Curl"] * 2
