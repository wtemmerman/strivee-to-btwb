"""The BTWB client against real BTWB pages, offline.

BTWB's own scripts cannot run here, so nothing is filled end to end; what is
checked is what broke live: every selector posting relies on is present on the
page it is used on, and the read-back script returns the right body for a saved
workout in each rendering. Fixtures come from tests/fixtures/btwb/refresh.py.
"""

import re
from pathlib import Path

import pytest

from strivee_btwb.btwb.classic import (
    _ALTERNATING_EMOM,
    _CLOCK_MINUTES,
    _INTERVAL_SECONDS,
    _REST_SECONDS,
    _SAVE_BUTTON,
    _SET_REPS,
    _SETS_PER_MOVEMENT,
    _TURN_NAMES,
    _TURN_REPS,
    _TURN_WEIGHT_UNITS,
    _TURN_WEIGHTS,
    _values,
)
from strivee_btwb.btwb.readback import _STORED_BODY_JS

FIXTURES = Path(__file__).parents[2] / "fixtures" / "btwb"

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        try:
            launched = pw.chromium.launch(headless=True)
        except sync_api.Error as exc:  # no browser installed: nothing to run against
            pytest.skip(f"chromium unavailable: {exc}")
        yield launched
        launched.close()


@pytest.fixture
def page_of(browser):
    pages = []

    def load(name: str):
        page = browser.new_page()
        page.route("**/*", lambda route: route.abort())  # the fixture only, never BTWB
        page.set_content((FIXTURES / f"{name}.html").read_text(), wait_until="domcontentloaded")
        pages.append(page)
        return page

    yield load
    for page in pages:
        page.close()


def test_the_save_button_is_exactly_one_button_on_every_template(page_of):
    """Live, a selector scoped inside the form matched nothing: the button sits outside it."""
    for template in (
        "for_distance",
        "weightlifting_sets",
        "gymnastics_sets",
        "rep_max",
        "single_emom",
        "alternating_emom",
    ):
        assert page_of(f"form_{template}").locator(_SAVE_BUTTON).count() == 1, template


def test_the_chooser_links_the_for_distance_template(page_of):
    assert page_of("template_chooser").locator("a[href$='/for_distance/new']").count() == 1


def test_for_distance_has_interval_rows_rest_and_set_controls(page_of):
    page = page_of("form_for_distance")
    rows = page.locator(_INTERVAL_SECONDS).count()
    assert rows >= 1
    assert page.locator(_CLOCK_MINUTES).count() == rows + 1  # one clock per row, one for rest
    assert page.locator(_REST_SECONDS).count() == 1
    for control in ("addSet", "removeSet"):
        assert page.locator(f"[data-action*='plan--sets-control#{control}']").count() == 1


def test_sets_templates_offer_the_schemes_and_loads_posting_selects(page_of):
    weighted = page_of("form_weightlifting_sets")
    assert weighted.locator("select#rep_scheme option[value='maxreps']").count() == 1
    assert (
        weighted.locator(
            "select[name='definition[prescription][weightPerSet]'] option[value='onerepmax']"
        ).count()
        == 1
    )
    assert weighted.locator(_SET_REPS).count() >= 1
    gymnastics = page_of("form_gymnastics_sets")
    assert gymnastics.locator("select#rep_scheme option[value='maxreps']").count() == 1


def test_rep_max_and_emom_hold_the_fields_posting_fills(page_of):
    assert page_of("form_rep_max").locator(_SET_REPS).count() == 1
    emom = page_of("form_single_emom")
    for field in ("every", "until"):
        assert emom.locator(f"input[name='definition[prescription][{field}][value]']").count() == 1
    assert emom.locator(_CLOCK_MINUTES).count() == 2
    assert emom.locator(_SET_REPS).count() == 1


def test_read_back_writes_out_a_lift_shown_as_the_editor(page_of):
    """The saved 10RM seal row renders as the editor, its values inside inputs."""
    body = page_of("event_editor_lift").evaluate(_STORED_BODY_JS)
    assert "10 Seal Row" in body.splitlines()


def test_read_back_skips_the_erg_editors_rpe_dropdown(page_of):
    """Live, the RPE scale read as twenty workout lines ("19 - 100% effort …")."""
    body = page_of("event_editor_erg").evaluate(_STORED_BODY_JS)
    assert not [line for line in body.splitlines() if re.match(r"^\d+ - .*effort", line)]
    assert "Bike Erg" in body


def test_read_back_keeps_a_metcon_summary(page_of):
    body = page_of("event_summary_metcon").evaluate(_STORED_BODY_JS)
    assert "40 Ski Erg Calories" in body.splitlines()


def test_the_new_workout_page_links_the_alternating_emom_template(page_of):
    assert page_of("new_workout").locator(_ALTERNATING_EMOM).count() == 1


def test_alternating_emom_holds_what_posting_fills_and_reads_back(page_of):
    """Captured with Pause Squat Clean and Squat Clean entered, 1 rep @ 70% 1RM each."""
    page = page_of("form_alternating_emom")
    assert page.locator(_SETS_PER_MOVEMENT).count() == 1
    assert page.locator(_CLOCK_MINUTES).count() >= 1
    for field in ("every", "until"):
        assert page.locator(f"input[name='definition[prescription][{field}][value]']").count() == 1
    assert _values(page, _TURN_NAMES) == ["Pause Squat Clean", "Squat Clean"]
    assert _values(page, _TURN_REPS) == ["1", "1"]
    assert _values(page, _TURN_WEIGHTS) == ["70", "70"]
    assert _values(page, _TURN_WEIGHT_UNITS) == ["onerepmax", "onerepmax"]
