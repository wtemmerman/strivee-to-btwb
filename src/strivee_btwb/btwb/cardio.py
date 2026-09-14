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
from datetime import date, datetime

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

_MAX_HISTORY_PAGES = 30
"""Pages of logged sessions to read before giving up on reaching the window's start.

A page holds fifteen sessions, so this covers well over a year of training. Hitting
it means the listing stopped going back, not that the history is long."""

_GARMIN_ACTIVITY_ID = re.compile(r"connect\.garmin\.com/modern/activity/(\d+)")

_SESSION_LIST_JS = """
() => [...document.querySelectorAll('li.workout_session')].map(li => ({
  date: (li.querySelector('.post-privacy-text')?.textContent || '').trim(),
  notes: li.querySelector('i')?.textContent || '',
  href: li.querySelector('.item_title a[href^="/workout_sessions/"]')?.getAttribute('href') || '',
}))
"""

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


def _one_line(description: str) -> str:
    """Collapse BTWB's multi-line description for a log line.

    Its first line only names the shape ("Intervals"), which the model already
    says; the efforts are what is worth reading back.
    """
    efforts = description.splitlines()[1:]
    return " · ".join(line.strip() for line in efforts if line.strip()) or description


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
    description = built.locator(".workout-description").inner_text().strip()
    return {
        "description": description,
        "shape": _one_line(description),
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


def _member_id(page: Page) -> str:
    href = page.locator("a[href^='/members/']").first.get_attribute("href")
    match = re.search(r"/members/(\d+)", href or "")
    if not match:
        raise BTWBError("Could not read the member id from the signed-in page")
    return match.group(1)


def _listed_date(text: str) -> date:
    """Read the date BTWB prints on a listed session, e.g. "September 13, 2026"."""
    try:
        return datetime.strptime(text, "%B %d, %Y").date()
    except ValueError as exc:
        raise BTWBError(
            f"Could not read {text!r} as a session date — is BTWB still set to English?"
        ) from exc


def fetch_synced_activity_ids(page: Page, since: date) -> dict[int, str]:
    """Which Garmin activities BTWB already holds, reading back to *since*.

    BTWB is the ledger. Every session this tool writes carries its Garmin link in
    the notes, so what has already been synced can be read out of the log itself.
    A file on this machine would drift the moment an entry was deleted on BTWB,
    and would have to be seeded by hand with everything logged before it existed.
    """
    member = _member_id(page)
    found: dict[int, str] = {}
    for number in range(1, _MAX_HISTORY_PAGES + 1):
        page.goto(
            f"{_BASE}/members/{member}/workout_sessions?page={number}", wait_until="networkidle"
        )
        items = page.evaluate(_SESSION_LIST_JS)
        if not items:
            return found
        for item in items:
            for activity_id in _GARMIN_ACTIVITY_ID.findall(item["notes"]):
                found[int(activity_id)] = f"{_BASE}{item['href']}"
        if min(_listed_date(i["date"]) for i in items if i["date"]) < since:
            return found
    raise BTWBError(f"Read {_MAX_HISTORY_PAGES} pages of history without reaching {since}")


def _already_synced(session: CardioSession, synced: dict[int, str]) -> str | None:
    """The BTWB entry already holding this session, if any of its activities is there.

    Any overlap counts as synced, not every one: a day whose ride count grew after
    it was logged would otherwise be posted a second time, and a duplicate is far
    worse than a merged entry that is one leg short.
    """
    held = [synced[i] for i in session.source_ids if i in synced]
    if held and len(held) != len(session.source_ids):
        logger.warning(
            "%s %r covers %d activities but BTWB holds only %d of them — left alone",
            session.date,
            session.title,
            len(session.source_ids),
            len(held),
        )
    return held[0] if held else None


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
        logger.info("Would log %s — %s | %s", session.date, built["shape"], built["result"])
        return result

    result.update(_submit(page))
    logger.info("Logged %s — %s → %s", session.date, built["result"], result.get("url", "saved"))
    return result


def sync_sessions(
    sessions: list[CardioSession],
    since: date,
    dry_run: bool = True,
    headless: bool = True,
) -> dict:
    """Log every session BTWB does not already hold, or walk the form without saving.

    Read first, write second, in one browser and one login. What BTWB holds is the
    only honest answer to what still needs logging, and a run that wrote before it
    read would duplicate everything it could not see. A failure on one session is
    raised with that session named rather than swallowed: a half-logged week the
    caller believes is complete is worse than a run that stops.
    """
    if not sessions:
        return {"logged": [], "skipped": []}
    logged: list[dict] = []
    skipped: list[dict] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        page = browser.new_context(viewport={"width": 1500, "height": 1300}).new_page()
        try:
            _login(page, config.BTWB_EMAIL, config.BTWB_PASSWORD)
            synced = fetch_synced_activity_ids(page, since)
            logger.info("BTWB already holds %d synced activity(ies) since %s", len(synced), since)
            for session in sessions:
                existing = _already_synced(session, synced)
                if existing:
                    skipped.append(
                        {
                            "date": session.date.isoformat(),
                            "title": session.title,
                            "source_ids": session.source_ids,
                            "url": existing,
                        }
                    )
                    continue
                try:
                    logged.append(_log_one(page, session, dry_run))
                except Exception as exc:
                    raise BTWBError(f"Failed on {session.date} {session.title!r}: {exc}") from exc
        finally:
            browser.close()
    return {"logged": logged, "skipped": skipped}
