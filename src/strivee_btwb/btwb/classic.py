"""Workouts built in BTWB's classic builder, field by field, instead of by its AI.

Posting runs in a French-locale browser, so everything here is found by URL or
form attribute, never by label text.
"""

import logging

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..core.btwb_names import confirm_movement
from ..core.models import ClassicSets, ProgrammingBlock
from .common import _FIELD_COMMIT_MS, _TIMEOUT, BTWBError

logger = logging.getLogger("btwb")


# ── Erg intervals (BTWB's classic builder) ────────────────────────────────────
#
# Posting runs in a French-locale browser, so everything here is found by URL or
# form attribute, never by label text.

_INTERVAL_SECONDS = "input[name='definition[contents][][time][value]']"
_REST_SECONDS = "input[name='definition[prescription][rest][value]']"
_CLOCK_MINUTES = "input[name='movement_time_value_minutes']"
# The save button sits outside the builder's form, tied to it by form=; the other
# button tied that way is a hidden preview, the one carrying a formaction.
_SAVE_BUTTON = "button[type='submit'][form='new_track_event']:not([formaction])"


def _fill_clock(minutes_field, seconds: int) -> None:
    """Fill a mins:secs pair; blurring it makes BTWB write the hidden seconds value."""
    minutes_field.fill(str(seconds // 60))
    seconds_field = minutes_field.locator("xpath=following::input[@type='number'][1]")
    seconds_field.fill(str(seconds % 60))
    seconds_field.press("Tab")


def _fill_erg_intervals(page: Page, block: ProgrammingBlock) -> Locator:
    """Build *block*'s "Intervals For Distance" workout and return its plan button.

    The hidden seconds BTWB submits are read back before planning: a blur that did
    not register would otherwise save an interval of nothing, silently.
    """
    plan = block.erg
    if plan is None:  # invariant: only called for blocks with an erg plan
        raise BTWBError(f"internal error: '{block.name}' has no erg plan")
    _open_classic_template(page, plan.movement, {"distance": "for_distance"})
    _set_row_count(page, _INTERVAL_SECONDS, len(plan.intervals))
    rows = page.locator(_INTERVAL_SECONDS)

    minutes = page.locator(_CLOCK_MINUTES)
    for i, seconds in enumerate(plan.intervals):
        _fill_clock(minutes.nth(i), seconds)
    _fill_clock(minutes.nth(len(plan.intervals)), plan.rest_seconds)
    page.wait_for_timeout(_FIELD_COMMIT_MS)

    entered = [int(v or -1) for v in rows.evaluate_all("els => els.map(e => e.value)")]
    rest = page.locator(_REST_SECONDS).input_value()
    if entered != list(plan.intervals) or rest != str(plan.rest_seconds):
        raise BTWBError(
            f"'{block.name}': BTWB holds intervals {entered} and rest {rest!r}, "
            f"not {list(plan.intervals)} and {plan.rest_seconds}"
        )
    logger.info("Entered %s for '%s'", block.content.splitlines()[-1], block.name)
    return page.locator(_SAVE_BUTTON).first


_SET_REPS = "input[name='definition[contents][][reps][value]']"


def _open_classic_template(page: Page, movement: str, templates: dict[str, str]) -> str:
    """Pick *movement* in the classic builder and open its template; return the kind.

    BTWB offers different templates by movement kind — the search link says which
    (".../single/reps/4045" for a bodyweight movement) — so *templates* maps a kind
    to the template to open.
    """
    page.locator("a[href='/plan/workouts/single']").first.click()
    search = page.locator("input#name")
    search.wait_for(state="visible", timeout=_TIMEOUT)
    search.fill(movement)
    link = page.get_by_role("link", name=movement, exact=True).first
    try:
        link.wait_for(state="visible", timeout=_TIMEOUT)
    except PlaywrightTimeoutError as exc:
        raise BTWBError(f"BTWB has no movement named {movement!r} — add it by hand") from exc
    confirm_movement(movement)
    kind = (link.get_attribute("href") or "").split("/single/")[-1].split("/")[0]
    if kind not in templates:
        raise BTWBError(f"'{movement}' is a {kind!r} movement; no classic template for it yet")
    link.click()
    template = page.locator(f"a[href$='/{templates[kind]}/new']").first
    template.wait_for(state="visible", timeout=_TIMEOUT)
    template.click()
    return kind


def _set_row_count(page: Page, row_selector: str, wanted: int) -> None:
    rows = page.locator(row_selector)
    rows.first.wait_for(state="attached", timeout=_TIMEOUT)
    for control, done in (
        ("addSet", lambda n: n >= wanted),
        ("removeSet", lambda n: n <= wanted),
    ):
        while not done(rows.count()):
            before = rows.count()
            page.locator(f"[data-action*='plan--sets-control#{control}']").first.click()
            page.wait_for_function(
                f'n => document.querySelectorAll("{row_selector}").length != n',
                arg=before,
                timeout=_TIMEOUT,
            )


_PERCENT_1RM = "input[name='definition[contents][][weight][value]']:visible"


def _values(page: Page, selector: str) -> list[str]:
    return page.locator(selector).evaluate_all("els => els.map(e => e.value)")


def _fill_rep_max(page: Page, block: ProgrammingBlock, plan: ClassicSets) -> None:
    _open_classic_template(page, plan.movement, {"weight": "rep_max"})
    reps = page.locator(_SET_REPS).first
    reps.wait_for(state="visible", timeout=_TIMEOUT)
    reps.fill(str(plan.reps[0]))
    reps.press("Tab")
    page.wait_for_timeout(_FIELD_COMMIT_MS)
    if _values(page, _SET_REPS) != [str(plan.reps[0])]:
        raise BTWBError(f"'{block.name}': BTWB holds reps {_values(page, _SET_REPS)}")


def _fill_emom(page: Page, block: ProgrammingBlock, plan: ClassicSets) -> None:
    if plan.emom_seconds is None:  # invariant: only called for EMOM plans
        raise BTWBError(f"internal error: '{block.name}' has no EMOM interval")
    _open_classic_template(page, plan.movement, {"reps": "single_emom", "weight": "single_emom"})
    clocks = page.locator(_CLOCK_MINUTES)
    clocks.first.wait_for(state="visible", timeout=_TIMEOUT)
    every, until = plan.emom_seconds, plan.emom_seconds * len(plan.reps)
    _fill_clock(clocks.nth(0), every)
    _fill_clock(clocks.nth(1), until)
    reps = page.locator(_SET_REPS).first
    reps.fill(str(plan.reps[0]))
    reps.press("Tab")
    page.wait_for_timeout(_FIELD_COMMIT_MS)
    entered = (
        page.locator("input[name='definition[prescription][every][value]']").input_value(),
        page.locator("input[name='definition[prescription][until][value]']").input_value(),
        _values(page, _SET_REPS),
    )
    if entered != (str(every), str(until), [str(plan.reps[0])]):
        raise BTWBError(f"'{block.name}': BTWB holds every/until/reps {entered}")


def _fill_counted_sets(page: Page, block: ProgrammingBlock, plan: ClassicSets) -> None:
    kind = _open_classic_template(
        page, plan.movement, {"reps": "gymnastics_sets", "weight": "weightlifting_sets"}
    )
    _set_row_count(page, _SET_REPS, len(plan.reps))
    if all(r is None for r in plan.reps):
        page.locator("select#rep_scheme").select_option("maxreps")
    elif any(r is None for r in plan.reps):
        raise BTWBError(f"'{block.name}': mixed max and counted sets are not supported yet")
    else:
        fields = page.locator(_SET_REPS)
        for i, reps in enumerate(plan.reps):
            fields.nth(i).fill(str(reps))
    if plan.percent_1rm is not None:
        if kind != "weight":
            raise BTWBError(f"'{block.name}': a % 1RM load on a bodyweight movement")
        page.locator("select[name='definition[prescription][weightPerSet]']").select_option(
            "onerepmax"
        )
        percents = page.locator(_PERCENT_1RM)
        percents.first.wait_for(state="visible", timeout=_TIMEOUT)
        for i in range(len(plan.reps)):
            percents.nth(i).fill(str(plan.percent_1rm))
            percents.nth(i).press("Tab")
    if plan.rest_seconds is not None:
        _fill_clock(page.locator(_CLOCK_MINUTES).last, plan.rest_seconds)
    page.wait_for_timeout(_FIELD_COMMIT_MS)

    expected = {
        "sets": len(plan.reps),
        "rest": "" if plan.rest_seconds is None else str(plan.rest_seconds),
        "scheme": "maxreps" if plan.reps[0] is None else "assign",
        "reps": [] if plan.reps[0] is None else [str(r) for r in plan.reps],
        "percent": [] if plan.percent_1rm is None else [str(plan.percent_1rm)] * len(plan.reps),
    }
    entered = {
        "sets": page.locator(_SET_REPS).count(),
        "rest": page.locator(_REST_SECONDS).input_value(),
        "scheme": page.locator("select#rep_scheme").input_value(),
        "reps": [] if plan.reps[0] is None else _values(page, _SET_REPS),
        "percent": [] if plan.percent_1rm is None else _values(page, _PERCENT_1RM),
    }
    if entered != expected:
        raise BTWBError(f"'{block.name}': BTWB holds {entered}, not {expected}")


def _fill_classic_sets(page: Page, block: ProgrammingBlock) -> Locator:
    """Build *block*'s classic Sets or X Rep Max workout and return its plan button.

    What BTWB holds is read back before planning: a field that did not take would
    otherwise save a different workout, silently.
    """
    plan = block.sets
    if plan is None:  # invariant: only called for blocks with a set plan
        raise BTWBError(f"internal error: '{block.name}' has no set plan")
    if plan.rep_max:
        _fill_rep_max(page, block, plan)
    elif plan.emom_seconds is not None:
        _fill_emom(page, block, plan)
    else:
        _fill_counted_sets(page, block, plan)
    logger.info("Entered %s for '%s'", block.content.splitlines()[-1], block.name)
    return page.locator(_SAVE_BUTTON).first
