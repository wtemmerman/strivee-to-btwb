"""
BTWB browser automation via Playwright.

Flow per day:
  1. Login (once per session)
  2. Navigate to /plan/track_events/workouts/new?d=DATE  (first block only)
  3. For each block:
       a. Fill the AI description textarea with the block text
       b. Click "Continuer" → BTWB AI parses the workout (two AJAX calls)
       c. Wait for "Planifier l'Entraînement" button to become enabled
       d. Fill custom title, click "Planifier"
       e. For the *next* block: if the current block saved, start a new workout
          from the "+" → "Nouvel entraînement" dropdown; otherwise (the AI failed
          to generate a preview, so no "+" exists) go straight to the new-workout
          URL again. A single failed block must never strand the rest of the day.
  4. After all days: navigate to /plan/calendar
"""

import logging
from collections.abc import Callable

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..core import config
from ..core.models import DayProgramming, ProgrammingBlock, WeeklyProgramming

logger = logging.getLogger("btwb")

_BASE = "https://beyondthewhiteboard.com"
_TIMEOUT = 30_000  # ms — element/AJAX waits (incl. BTWB AI workout generation)
_CALENDAR_IDLE_TIMEOUT = 10_000  # ms — networkidle cap for calendar pages (non-fatal)


class BTWBError(Exception):
    pass


class AuthenticationError(BTWBError):
    pass


# ── Pure helpers (no browser — unit-testable) ─────────────────────────────────


def _calendar_week_url(date_str: str) -> str:
    """Build the BTWB calendar-week URL for an ISO date, dropping zero-padding."""
    year, month, day = date_str.split("-")
    return f"{_BASE}/plan/calendar/week/{int(year)}/{int(month)}/{int(day)}"


def _calendar_month_url(date_str: str) -> str:
    """Build the BTWB calendar-month URL for an ISO date, dropping zero-padding.

    The month view needs a day segment (always 1) — without it, /month resolves
    to the *current* month regardless of the year/month given.
    """
    year, month, _day = date_str.split("-")
    return f"{_BASE}/plan/calendar/month/{int(year)}/{int(month)}/1"


def _group_dates_by_month(dates: list[str]) -> dict[tuple[int, int], list[str]]:
    """Group ISO date strings by (year, month) so each month is loaded once.

    A single week can straddle two months, so deletion scans the month view for
    every month the requested dates touch, not just one.
    """
    by_month: dict[tuple[int, int], list[str]] = {}
    for d in dates:
        year, month, _day = d.split("-")
        by_month.setdefault((int(year), int(month)), []).append(d)
    return by_month


def _blocks_to_post(day: DayProgramming, existing: set[str]) -> list[ProgrammingBlock]:
    """Return the day's blocks whose titles are not already planned on BTWB."""
    return [b for b in day.blocks if b.name not in existing]


def _planned_result(block: ProgrammingBlock, date_str: str) -> dict:
    """Build the dry-run record describing a block that would be posted."""
    return {"dry_run": True, "block": block.name, "date": date_str}


def _login(page: Page, email: str, password: str) -> None:
    page.goto(f"{_BASE}/signin", wait_until="domcontentloaded")
    page.locator("input[name='login']").fill(email)
    page.locator("input[name='password']").fill(password)
    page.locator("input[type='submit'], button[type='submit']").first.click()
    page.wait_for_url(lambda url: "signin" not in url, timeout=_TIMEOUT)
    if "signin" in page.url:
        raise AuthenticationError(
            "Login failed — still on signin page. Check BTWB_EMAIL / BTWB_PASSWORD in .env."
        )


def _add_instruction(page: Page, block: ProgrammingBlock) -> None:
    """Open the Instructions tab and submit the coaching note for a planned block.

    Skipped silently when block.instruction is empty or the tab is not found
    (e.g. older BTWB page variants).
    """
    if not block.instruction:
        return
    try:
        tab = page.locator("[data-bs-target='#athlete-instructions']")
        tab.wait_for(state="visible", timeout=5_000)
        tab.click()

        panel = page.locator("#athlete-instructions")
        panel.wait_for(state="visible", timeout=_TIMEOUT)

        panel.locator("input[name='track_event_instruction[title]']").fill(block.name)
        panel.locator("textarea[name='track_event_instruction[body]']").fill(block.instruction)

        save_btn = panel.locator("button:has-text('Enregistrer la note'):not([disabled])")
        save_btn.wait_for(state="visible", timeout=_TIMEOUT)
        save_btn.click()
        page.wait_for_load_state("networkidle", timeout=_TIMEOUT)
        logger.info("Instruction saved for block '%s'", block.name)
    except PlaywrightTimeoutError:
        logger.warning("Block '%s' — instruction tab not found or timed out, skipping", block.name)


def _fill_and_plan(page: Page, block: ProgrammingBlock, last_block: bool) -> None:
    """Fill the workout description, submit, wait for preview, then click Planifier."""
    # Select the first track (Piste) if not already pre-selected by the URL
    track_select = page.locator("select[name='track_event[track_id]']")
    if track_select.count() and not track_select.input_value():
        track_select.select_option(index=1)
        page.wait_for_load_state("networkidle", timeout=_TIMEOUT)

    description_field = page.locator(
        "textarea[name='planning_generated_workout[external_description]']"
    )
    description_field.wait_for(timeout=_TIMEOUT)
    description_field.fill(block.content)

    # Set up response listeners before the click so fast responses aren't missed
    with (
        page.expect_response(lambda r: "generated_workouts" in r.url, timeout=_TIMEOUT) as _gen,
        page.expect_response(lambda r: "track_events" in r.url, timeout=_TIMEOUT) as _track,
    ):
        page.locator(
            "input[type='submit'][value='Continuer'], input[type='submit'][value='Continue']"
        ).first.click()

    # Wait for Planifier button to become enabled (disabled while preview loads)
    plan_button = page.locator("button:has-text('Planifier'):not([disabled])")
    plan_button.wait_for(state="visible", timeout=_TIMEOUT)

    title_field = page.locator("input[name='track_event[title]']")
    if title_field.count():
        title_field.fill(block.name)

    plan_button.click()

    # "+" button appearing signals the workout was saved and the page is ready
    plus_button = page.locator(
        "button.btn-outline-grey-200[data-bs-toggle='dropdown']:not([disabled])"
    ).first
    plus_button.wait_for(state="visible", timeout=_TIMEOUT)

    _add_instruction(page, block)

    if last_block:
        # The save is already confirmed (the "+" button appeared). Settle briefly,
        # but never raise here — a timeout would make the caller mis-mark this
        # saved block as skipped.
        try:
            page.wait_for_load_state("networkidle", timeout=_CALENDAR_IDLE_TIMEOUT)
        except PlaywrightTimeoutError:
            pass

    logger.info("Block '%s' saved", block.name)


def _navigate_to_new_workout(page: Page, date_str: str, via_plus: bool) -> None:
    """Open a fresh workout form for ``date_str``.

    Once a workout is saved, BTWB keeps it on screen, so the next one must be
    started from the "+" → "Nouvel entraînement" dropdown (``via_plus=True``).
    The first block — and any block whose predecessor failed to save, leaving no
    "+" button — goes straight to the new-workout URL instead. The "+" path falls
    back to that same URL if the dropdown never appears, so one failed block can't
    strand the remaining blocks of the day.
    """
    direct_url = f"{_BASE}/plan/track_events/workouts/new?d={date_str}"
    if not via_plus:
        page.goto(direct_url, wait_until="domcontentloaded")
        return
    try:
        plus_button = page.locator(
            "button.btn-outline-grey-200[data-bs-toggle='dropdown']:not([disabled])"
        ).first
        plus_button.wait_for(state="visible", timeout=_TIMEOUT)
        plus_button.click()
        new_link = page.locator("a.dropdown-item:has-text('Nouvel entraînement')")
        new_link.wait_for(state="attached", timeout=_TIMEOUT)
        href = new_link.get_attribute("href")
        page.goto(f"{_BASE}{href}", wait_until="domcontentloaded")
    except PlaywrightTimeoutError:
        logger.warning(
            "'+' dropdown unavailable — falling back to direct new-workout URL for %s",
            date_str,
        )
        page.goto(direct_url, wait_until="domcontentloaded")


def _ensure_track_selected(page: Page) -> None:
    """Check the personal-track checkbox so its workouts render on the calendar.

    BTWB only shows a track's workouts when its sidebar checkbox is ticked, so
    both reading existing workouts and finding workouts to delete depend on this.
    Skipped when BTWB_TRACK_ID is unset. wait_for(attached) is required — the
    checkbox is injected by JS after domcontentloaded, so count() would return 0
    and the click would be silently skipped without this wait.
    """
    if not config.BTWB_TRACK_ID:
        return
    track_cb = page.locator(f"#plan_track_{config.BTWB_TRACK_ID}")
    track_cb.wait_for(state="attached", timeout=_TIMEOUT)
    if not track_cb.is_checked():
        track_cb.click()


# Scan every requested date's calendar container in one pass. The week view holds
# all 7 day containers, so titles for the whole week come from a single load.
_SCAN_TITLES_JS = """
(dates) => {
  const out = {};
  dates.forEach(d => { out[d] = []; });
  document.querySelectorAll('[data-date]').forEach(el => {
    const raw = el.getAttribute('data-date') || '';
    const d = dates.find(x => raw.includes(x));
    if (!d) return;
    el.querySelectorAll('.title_track_event strong').forEach(s => {
      const t = (s.getAttribute('title') || s.textContent || '').trim();
      if (t) out[d].push(t);
    });
  });
  return out;
}
"""


def _settle_calendar(page: Page) -> None:
    """Wait for the calendar day containers, then best-effort settle AJAX.

    The networkidle wait is capped and non-fatal: titles are already in the DOM
    once [data-date] is present, so a slow/long-polling calendar must not abort
    posting or block for the full element timeout.
    """
    page.wait_for_selector("[data-date]", timeout=_TIMEOUT)
    try:
        page.wait_for_load_state("networkidle", timeout=_CALENDAR_IDLE_TIMEOUT)
    except PlaywrightTimeoutError:
        logger.warning(
            "Calendar still loading after %ds — proceeding", _CALENDAR_IDLE_TIMEOUT // 1000
        )


def _fetch_existing_titles_for_week(page: Page, dates: list[str]) -> dict[str, set[str]]:
    """Return {date: planned workout titles} for the whole week in ONE load.

    Replaces the previous per-day calendar navigation: the week view already
    contains every day's container, so one load + one scan covers all dates.
    """
    if not dates:
        return {}
    # bring_to_front prevents macOS background-tab JS throttling when the
    # terminal has focus (e.g. user just typed at the confirmation prompt).
    page.bring_to_front()
    page.goto(_calendar_week_url(dates[0]), wait_until="domcontentloaded")
    _ensure_track_selected(page)
    _settle_calendar(page)
    titles_by_date: dict[str, list[str]] = page.evaluate(_SCAN_TITLES_JS, dates)
    result = {d: set(titles_by_date.get(d, [])) for d in dates}
    for d in dates:
        logger.info("Existing blocks on BTWB for %s: %s", d, sorted(result[d]) or "none")
    return result


def _fetch_existing_block_names(page: Page, date_str: str) -> set[str]:
    """Return workout titles already planned for a single date on BTWB.

    Kept for the single-day path; post_week now batches the whole week via
    _fetch_existing_titles_for_week to avoid one calendar load per day.
    """
    return _fetch_existing_titles_for_week(page, [date_str]).get(date_str, set())


def _post_day(
    page: Page | None,
    day: DayProgramming,
    dry_run: bool,
    existing: set[str] | None = None,
) -> list[dict]:
    date_str = day.date.isoformat()
    logger.info("%s %s — %d block(s)", day.day_label, date_str, len(day.blocks))

    if dry_run:
        for block in day.blocks:
            logger.info("[dry-run] Would submit '%s': %s...", block.name, block.content[:60])
        return [_planned_result(b, date_str) for b in day.blocks]

    if page is None:  # invariant: the non-dry-run path always gets a live page
        raise BTWBError("internal error: _post_day called without a page (dry_run=False)")
    # post_week passes the week's existing titles (one calendar load); fall back to
    # a single-day fetch when called directly without them.
    if existing is None:
        existing = _fetch_existing_block_names(page, date_str)
    if existing:
        logger.info("Already on BTWB: %s", ", ".join(sorted(existing)))

    blocks_to_post = _blocks_to_post(day, existing)
    if not blocks_to_post:
        logger.info("%s — all blocks already posted, skipping", day.day_label)
        return []

    results: list[dict] = []
    # The "+" dropdown only exists after a block saves successfully, so the next
    # block may only navigate via "+" when its predecessor saved. The first block
    # and any block following a failure go straight to the new-workout URL.
    prev_saved = False
    for i, block in enumerate(blocks_to_post):
        logger.info("Submitting block '%s'", block.name)
        is_last = i == len(blocks_to_post) - 1

        _navigate_to_new_workout(page, date_str, via_plus=prev_saved)

        try:
            _fill_and_plan(page, block, last_block=is_last)
            results.append({"block": block.name, "date": date_str, "ok": True})
            prev_saved = True
        except PlaywrightTimeoutError:
            logger.warning("Block '%s' skipped — BTWB AI did not generate a preview", block.name)
            results.append({"block": block.name, "date": date_str, "skipped": True})
            prev_saved = False

    return results


# ── Deletion ──────────────────────────────────────────────────────────────────

# Scans the calendar month grid for *planned* workouts (track events) on the
# given dates and returns the data needed to delete each one. Completed workout
# sessions are skipped: only planned track events carry the delete form (a Rails
# DELETE — POST with _method=delete + the page's CSRF token), so submitting that
# form is exactly what clicking "Supprimer" → "Oui, supprimer" does in the UI.
_SCAN_DELETABLE_JS = """
(dates) => {
  const out = [];
  document.querySelectorAll('[data-date]').forEach(container => {
    const raw = container.getAttribute('data-date') || '';
    const matched = dates.find(d => raw.includes(d));
    if (!matched) return;
    container.querySelectorAll("div[data-controller='plan--track-event']").forEach(ev => {
      const del = ev.querySelector("input[name='_method'][value='delete']");
      const form = del && del.closest('form');
      if (!form) return;
      const action = form.getAttribute('action') || '';
      const tokenEl = form.querySelector("input[name='authenticity_token']");
      const titleEl = ev.querySelector("a .flex-fill");
      const idMatch = action.match(/(\\d+)\\s*$/);
      out.push({
        date: matched,
        id: idMatch ? idMatch[1] : '',
        action,
        token: tokenEl ? tokenEl.value : '',
        title: titleEl ? titleEl.textContent.trim() : '',
      });
    });
  });
  return out;
}
"""


def _collect_deletable_events(page: Page, dates: list[str]) -> list[dict]:
    """Return planned (deletable) workouts on the given ISO dates.

    Each entry is {date, id, action, token, title}. Loads the month view once per
    month the dates span; events are matched by an exact data-date hit, so a wrong
    or empty month simply yields nothing rather than touching unrelated days.
    """
    events: list[dict] = []
    for (_year, _month), month_dates in _group_dates_by_month(dates).items():
        # bring_to_front avoids macOS background-tab JS throttling (same reason as
        # _fetch_existing_block_names) while the calendar's AJAX content loads.
        page.bring_to_front()
        page.goto(_calendar_month_url(month_dates[0]), wait_until="domcontentloaded")
        # Select the track first, otherwise its workouts never render and the
        # scan finds nothing to delete.
        _ensure_track_selected(page)
        page.wait_for_selector("[data-date]", timeout=_TIMEOUT)
        page.wait_for_load_state("networkidle", timeout=_TIMEOUT)
        events.extend(page.evaluate(_SCAN_DELETABLE_JS, month_dates))
    return events


def _delete_event(page: Page, event: dict) -> bool:
    """Submit the Rails DELETE for one planned workout via the authed session.

    Uses the browser context's request API so the session cookie and the form's
    own CSRF token travel with the POST — equivalent to clicking "Oui, supprimer".
    """
    action = event["action"]
    url = action if action.startswith("http") else f"{_BASE}{action}"
    response = page.request.post(
        url, form={"_method": "delete", "authenticity_token": event["token"]}
    )
    label = event["title"] or f"id {event['id']}"
    if response.status >= 400:
        logger.warning(
            "Failed to delete '%s' (%s) — HTTP %d", label, event["date"], response.status
        )
        return False
    logger.info("Deleted '%s' (%s)", label, event["date"])
    return True


def delete_week(
    email: str,
    password: str,
    dates: list[str],
    dry_run: bool = False,
    headless: bool = False,
    confirm: Callable[[list[dict]], bool] | None = None,
) -> list[dict]:
    """Delete every planned workout on the given ISO dates from BTWB.

    Logs in, scans the calendar for deletable workouts on ``dates``, optionally
    asks ``confirm`` to proceed, then deletes each one. With ``dry_run`` the
    workouts that would be deleted are logged and returned without deleting.
    Completed sessions (which have no delete option) are never touched.
    """
    results: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(locale="fr-FR")
        page = context.new_page()

        logger.info("Logging in...")
        _login(page, email, password)
        logger.info("Authenticated")

        events = _collect_deletable_events(page, dates)
        for e in events:
            logger.info("Found: %s — %s", e["date"], e["title"] or f"id {e['id']}")

        if not events:
            logger.info("No deletable workouts found for: %s", ", ".join(dates))
            browser.close()
            return []

        if dry_run:
            logger.info("[dry-run] Would delete %d workout(s)", len(events))
            browser.close()
            return [
                {"date": e["date"], "title": e["title"], "id": e["id"], "dry_run": True}
                for e in events
            ]

        if confirm is not None and not confirm(events):
            logger.info("Deletion cancelled — nothing was removed.")
            browser.close()
            return []

        for e in events:
            ok = _delete_event(page, e)
            results.append({"date": e["date"], "title": e["title"], "id": e["id"], "ok": ok})

        deleted = sum(1 for r in results if r["ok"])
        logger.info("Deleted %d of %d workout(s) — opening calendar", deleted, len(results))
        page.goto(f"{_BASE}/plan/calendar", wait_until="domcontentloaded")
        browser.close()

    return results


def post_week(
    week: WeeklyProgramming,
    email: str,
    password: str,
    days: list[DayProgramming] | None = None,
    dry_run: bool = False,
    headless: bool = False,
) -> list[dict]:
    days_to_post = days if days is not None else week.days

    if dry_run:
        all_results = []
        for day in days_to_post:
            all_results.extend(_post_day(None, day, dry_run=True))
        return all_results

    all_results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(locale="fr-FR")
        page = context.new_page()

        logger.info("Logging in...")
        _login(page, email, password)
        logger.info("Authenticated")

        # One calendar load for the whole week instead of one per day.
        existing_by_date = _fetch_existing_titles_for_week(
            page, [day.date.isoformat() for day in days_to_post]
        )

        for day in days_to_post:
            existing = existing_by_date.get(day.date.isoformat(), set())
            all_results.extend(_post_day(page, day, dry_run=False, existing=existing))

        logger.info("All done — opening calendar")
        page.goto(f"{_BASE}/plan/calendar", wait_until="domcontentloaded")

        browser.close()

    return all_results
