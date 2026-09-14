"""Unit tests for the parts of the cardio writer that decide, not the parts that click.

The form walk itself is exercised against BTWB by a dry run; what is pinned here
is the arithmetic and the rest rule, which are the two places a wrong number
would be entered confidently.
"""

from datetime import date

import pytest

from strivee_btwb.btwb import cardio
from strivee_btwb.core.models import INTERVALS, SINGLE_DISTANCE, CardioInterval, CardioSession


class FakeLocator:
    """Records what the writer would have done to the form."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def locator(self, selector):
        self.calls.append(("locator", selector))
        return self

    @property
    def first(self):
        return self

    def select_option(self, value):
        self.calls.append(("select", value))

    def fill(self, value):
        self.calls.append(("fill", value))


def _session(*intervals):
    return CardioSession(
        date=date(2026, 8, 5),
        movement="Run",
        model=INTERVALS,
        title="5 x 500m",
        distance_m=8990,
        duration_s=3067,
        intervals=list(intervals),
    )


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(118.0, ("1", "58")), (3067.0, ("51", "07")), (5425.0, ("90", "25")), (0.0, ("0", "00"))],
)
def test_durations_split_into_the_two_boxes_btwb_offers(seconds, expected):
    """Minutes are never wrapped into hours — the form has no hours box."""
    assert cardio._clock_parts(seconds) == expected


def test_the_rest_entered_is_the_mean_of_what_was_actually_taken():
    form = FakeLocator()
    cardio._set_rest(
        form,
        _session(
            CardioInterval(500, 118, rest_s=77.0),
            CardioInterval(500, 122, rest_s=80.0),
            CardioInterval(500, 116, rest_s=89.0),
        ),
    )
    assert ("select", "xtime") in form.calls
    assert [c for c in form.calls if c[0] == "fill"] == [("fill", "1"), ("fill", "22")]


def test_a_session_with_no_recorded_rest_says_as_needed():
    form = FakeLocator()
    cardio._set_rest(form, _session(CardioInterval(400, 90), CardioInterval(400, 92)))
    assert ("select", "asneeded") in form.calls
    assert not [c for c in form.calls if c[0] == "fill"]


def test_the_last_rep_carrying_no_rest_does_not_drag_the_mean_down():
    form = FakeLocator()
    cardio._set_rest(
        form,
        _session(
            CardioInterval(500, 118, rest_s=80.0),
            CardioInterval(500, 122, rest_s=80.0),
            CardioInterval(500, 116, rest_s=None),
        ),
    )
    assert [c for c in form.calls if c[0] == "fill"] == [("fill", "1"), ("fill", "20")]


def test_both_models_have_a_label_to_click():
    assert set(cardio._MODEL_LABELS) == {SINGLE_DISTANCE, INTERVALS}


def test_syncing_nothing_opens_no_browser():
    assert cardio.sync_sessions([], date(2026, 9, 7)) == {"logged": [], "skipped": []}


def test_the_log_line_keeps_the_efforts_and_drops_the_shape_word():
    assert cardio._one_line("Intervals\nRun, 500 m | 1:58\nRun, 500 m | 2:03") == (
        "Run, 500 m | 1:58 · Run, 500 m | 2:03"
    )


def test_a_description_with_nothing_but_a_shape_word_is_kept_as_is():
    assert cardio._one_line("Intervals") == "Intervals"
