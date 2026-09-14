"""Log completed runs and rides into BTWB's workout logger.

BTWB has no import for a watch, so a session is entered the way a person enters
it: Log → Single Movement Workout → the movement → the scoring model → the
numbers → date and notes. The form is Knockout-bound, which decides how this
module drives it — every field is reached through the markup's own class names
(`fieldset.distance-input`, `fieldset.total-time-input`, `.edit-rest`) rather
than by position, because the inputs carry no names and their order changes with
the model on screen.

Two controls do not accept a value directly. The interval count is a jQuery
spinner whose input is readonly, so it is stepped with its own up-arrow, once per
rep beyond the first two. The date is a jQuery datepicker whose display format is
its own business, so it is set through the datepicker rather than by typing a
string this module would have to guess the format of.

Nothing here decides *what* to log. The sessions arrive already mapped, and
whether one has been logged before is the caller's business — this module fills
the form and reports what BTWB made of it.
"""

import logging
import re

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..core import config
from ..core.models import INTERVALS, SINGLE_DISTANCE, CardioSession
from ..processing.garmin_map import same_each_interval
from .client import _BASE, _TIMEOUT, BTWBError, _login

logger = logging.getLogger("btwb")

_LOGGER_URL = f"{_BASE}/workouts/logger#single"

_MODEL_LABELS = {
    SINGLE_DISTANCE: "Single Distance",
    INTERVALS: "Intervals / Repeats",
}

_REST_AS_NEEDED = "asneeded"
_REST_SPECIFY = "xtime"

_SAVE_REDIRECT_TIMEOUT = 15_000
"""How long to wait for BTWB's own script to move to the session it just saved."""

_SPINNER_FLOOR = 2
"""Interval count the form opens with — the spinner cannot go below it."""

_SET_DATE_JS = """
(iso) => {
  const [y, m, d] = iso.split('-').map(Number);
  const field = window.jQuery('#performedOn');
  field.datepicker('setDate', new Date(y, m - 1, d));
  field.trigger('change');
  return field.val();
}
"""


def _form(page: Page):
    """The model's field group, whichever of the two is on screen."""
    return page.locator("[class*='logger-distance']").locator("visible=true").first


def _clock_parts(seconds: float) -> tuple[str, str]:
    """Split a duration into the minutes and seconds boxes BTWB asks for.

    Minutes are not wrapped into hours: the form has no hours box, and a 90-minute
    ride is entered as 90 minutes.
    """
    total = round(seconds)
    return str(total // 60), f"{total % 60:02d}"


def _fill_time(scope, mins: str, secs: str) -> None:
    scope.locator("input[placeholder='mins']").first.fill(mins)
    scope.locator("input[placeholder='secs']").first.fill(secs)


def _fill_distance(scope, value: str, unit: str) -> None:
    scope.locator("input[placeholder='distance']").first.fill(value)
    scope.locator("select").first.select_option(unit)


def _pick_movement(page: Page, name: str) -> None:
    box = page.locator("input[placeholder='find your movement']").first
    box.click()
    box.type(name, delay=50)
    # Anchored: "Run" must not select "Shuttle Run", which sorts into the same list.
    option = page.locator(".ui-autocomplete li").filter(
        has_text=re.compile(rf"^{re.escape(name)}$")
    )
    option.first.wait_for(state="visible", timeout=_TIMEOUT)
    option.first.click()


def _open_logger(page: Page, session: CardioSession) -> None:
    """Load a clean logger and walk it as far as the model's field group.

    The reload is not redundant: the logger routes on the URL hash, so returning
    to it after a save would re-show the previous workout's state instead of
    building a new one.
    """
    page.goto(_LOGGER_URL, wait_until="networkidle")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("input[placeholder='find your movement']", timeout=_TIMEOUT)
    _pick_movement(page, session.movement)
    label = _MODEL_LABELS[session.model]
    page.get_by_text(label, exact=True).first.wait_for(state="visible", timeout=_TIMEOUT)
    page.get_by_text(label, exact=True).first.click()
    _form(page).wait_for(state="visible", timeout=_TIMEOUT)


def _fill_single_distance(page: Page, session: CardioSession) -> None:
    form = _form(page)
    _fill_distance(form, f"{session.distance_m / 1000:.2f}", "km")
    _fill_time(form.locator("fieldset.total-time-input"), *_clock_parts(session.duration_s))


def _set_interval_count(form, count: int) -> None:
    up = form.locator("fieldset.intervals-input a.ui-spinner-up")
    for _ in range(count - _SPINNER_FLOOR):
        up.click()
    rows = form.locator("fieldset.distance-intervals-input ol > li")
    if rows.count() != count:
        raise BTWBError(f"Asked for {count} intervals, the form shows {rows.count()}")


def _set_rest(form, session: CardioSession) -> None:
    """Enter the rest actually taken, or say it was as needed when none was recorded.

    Recorded rests vary rep to rep and BTWB stores one value, so the mean is what
    goes in — "Specify time" rather than the nearest preset, because a measured
    1:22 is worth more than a tidy 1:30.
    """
    rests = [i.rest_s for i in session.intervals if i.rest_s]
    select = form.locator(".edit-rest select")
    if not rests:
        select.select_option(_REST_AS_NEEDED)
        return
    select.select_option(_REST_SPECIFY)
    _fill_time(form.locator(".edit-rest .specify-time"), *_clock_parts(sum(rests) / len(rests)))


def _fill_intervals(page: Page, session: CardioSession) -> None:
    form = _form(page)
    uniform = same_each_interval(session.intervals)
    form.locator("select").filter(has_text="Varied each interval").first.select_option(
        "same" if uniform else "vary"
    )
    _set_interval_count(form, len(session.intervals))

    if uniform:
        _fill_distance(
            form.locator(".interval-distance"), str(round(session.intervals[0].distance_m)), "m"
        )
    rows = form.locator("fieldset.distance-intervals-input ol > li")
    for index, interval in enumerate(session.intervals):
        row = rows.nth(index)
        if not uniform:
            _fill_distance(
                row.locator("fieldset.distance-input"), str(round(interval.distance_m)), "m"
            )
        _fill_time(row.locator("fieldset.time-input"), *_clock_parts(interval.duration_s))

    _set_rest(form, session)
    _fill_time(form.locator("fieldset.total-time-input"), *_clock_parts(session.duration_s))


def _confirmation(page: Page, session: CardioSession) -> dict:
    """Read back what BTWB built, on the page that is about to submit it.

    This is the check that the form was filled right, and it is taken here rather
    than from the builder's heading because this page shows the numbers BTWB will
    actually store — every effort, the scored total, and the date in the hidden
    field the form posts.
    """
    built = page.locator(".built-result")
    performed = page.locator("#workout_session_performedDate").input_value()
    if performed != session.date.isoformat():
        raise BTWBError(f"BTWB would file this under {performed}, not {session.date.isoformat()}")
    return {
        "description": built.locator(".workout-description").inner_text().strip(),
        "result": built.locator("h3").inner_text().strip(),
        "performed_on": performed,
        "notes": page.locator("#workout_session_notes").input_value(),
    }


def _raise_on_save_dialog(page: Page) -> None:
    alert = page.locator("#dialog-alert:visible")
    if alert.count():
        raise BTWBError(f"BTWB refused the entry: {alert.inner_text().strip()}")


def _fill_date_and_notes(page: Page, session: CardioSession) -> None:
    page.locator("a.workout-logger-next").first.click()
    page.wait_for_selector("#performedOn", timeout=_TIMEOUT)
    _raise_on_save_dialog(page)
    shown = page.evaluate(_SET_DATE_JS, session.date.isoformat())
    logger.debug("Performed on set to %s for %s", shown, session.title)
    page.locator("#workout_session_notes").fill(session.notes)


def _submit(page: Page) -> dict:
    """Save the entry, and confirm it from the POST rather than from the click.

    The form posts with Rails' data-remote, so the click itself navigates nowhere
    and the page still reads /workouts/logger a moment later, saved or not. The
    POST's status is the first answer; the script BTWB sends back then moves the
    browser to the new session, which is where its id comes from. The response
    body cannot be read for it — by the time it could be asked for, that
    navigation has already discarded it.
    """
    with page.expect_response(
        lambda r: r.request.method == "POST" and "/workouts/logger" in r.url,
        timeout=_TIMEOUT,
    ) as caught:
        page.locator("input[name='commit']").first.click()
    response = caught.value
    if not response.ok:
        raise BTWBError(f"BTWB rejected the entry: HTTP {response.status}")
    try:
        page.wait_for_url(re.compile(r"/workout_sessions/\d+"), timeout=_SAVE_REDIRECT_TIMEOUT)
    except PlaywrightTimeoutError:
        # The POST succeeded, so the session exists; only its address is unknown.
        _raise_on_save_dialog(page)
        logger.warning("Saved, but BTWB did not move to the new session")
        return {"saved": True}
    return {"saved": True, "url": page.url}


def _log_one(page: Page, session: CardioSession, dry_run: bool) -> dict:
    _open_logger(page, session)
    if session.model == INTERVALS:
        _fill_intervals(page, session)
    else:
        _fill_single_distance(page, session)
    _fill_date_and_notes(page, session)
    built = _confirmation(page, session)

    result = {
        "date": session.date.isoformat(),
        "title": session.title,
        "movement": session.movement,
        "model": session.model,
        "source_ids": session.source_ids,
        "dry_run": dry_run,
        **built,
    }
    if dry_run:
        logger.info("Would log %s — %s | %s", session.date, built["description"], built["result"])
        return result

    result.update(_submit(page))
    logger.info("Logged %s — %s → %s", session.date, built["result"], result.get("url", "saved"))
    return result


def log_sessions(
    sessions: list[CardioSession], dry_run: bool = True, headless: bool = True
) -> list[dict]:
    """Enter each session in BTWB's logger, or walk the form without saving.

    One browser, one login, one session after another: a failure on one is raised
    with the session named rather than swallowed, because a half-logged week the
    caller believes is complete is worse than a run that stops.
    """
    if not sessions:
        return []
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        page = browser.new_context(viewport={"width": 1500, "height": 1300}).new_page()
        try:
            _login(page, config.BTWB_EMAIL, config.BTWB_PASSWORD)
            for session in sessions:
                try:
                    results.append(_log_one(page, session, dry_run))
                except Exception as exc:
                    raise BTWBError(f"Failed on {session.date} {session.title!r}: {exc}") from exc
        finally:
            browser.close()
    return results
