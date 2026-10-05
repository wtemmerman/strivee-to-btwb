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
import re
from collections.abc import Callable
from datetime import date, timedelta

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..core.models import DayProgramming, ProgrammingBlock, WeeklyProgramming
from .classic import (
    _ROW_COUNT_JS,
    _add_movement,
    _fill_alternating_emom,
    _fill_classic_sets,
    _fill_erg_intervals,
    _movement_row_count,
)
from .common import (
    _BASE,
    _CALENDAR_IDLE_TIMEOUT,
    _TIMEOUT,
    BTWBError,
    _calendar_week_url,
    _ensure_track_selected,
    _login,
    _settle_calendar,
)

logger = logging.getLogger("btwb")

# ── Pure helpers (no browser — unit-testable) ─────────────────────────────────


def _group_dates_by_week(dates: list[str]) -> dict[str, list[str]]:
    """Group ISO date strings by the Monday of their week, so each loads once.

    Deletion scans the week view rather than the month view: the month view keeps
    its day containers in the DOM but not visible, so waiting for one to appear
    times out and no workout is ever found to delete.
    """
    by_week: dict[str, list[str]] = {}
    for d in dates:
        day = date.fromisoformat(d)
        monday = (day - timedelta(days=day.weekday())).isoformat()
        by_week.setdefault(monday, []).append(d)
    return by_week


def _blocks_to_post(day: DayProgramming, existing: set[str]) -> list[ProgrammingBlock]:
    """Return the day's blocks whose titles are not already planned on BTWB."""
    return [b for b in day.blocks if b.name not in existing]


def _planned_result(block: ProgrammingBlock, date_str: str) -> dict:
    """Build the dry-run record describing a block that would be posted."""
    return {"dry_run": True, "block": block.name, "date": date_str}


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


# ── Exact movement entry (bypasses BTWB's AI text parser) ─────────────────────
#
# The workout textarea is parsed by BTWB's AI, which resolves a movement it does
# not recognise to an arbitrary other one instead of failing: "Cable Lateral
# Raise" came back as "Clean Deadlift W/ Pause At Mid Shin", "Pec Deck" as "Pause
# Power Clean & Jerks". Every one of those movements exists in BTWB's database and
# its own search finds them by the exact name — only the text parser misses them.
# So accessory blocks are entered through that search instead, one movement at a
# time. It costs a round-trip per set and cannot log the wrong exercise.

_MOVEMENT_LINE = re.compile(r"^\s*(\d+)\s+(\S.*?)\s*$")

# The picker needs a workout to attach movements to, and the AI textarea is the
# only way to create one. This seed resolves reliably and is deleted once the real
# movements are in.
_SEED_DESCRIPTION = "12 Dumbbell Curl"

_DELETE_LABEL = "SUPPRIMER"
_COPY_LABEL = "COPIER"


def _parse_movement_lines(block: ProgrammingBlock) -> list[tuple[str, str]]:
    """Read a block written as one ``"<reps> <movement>"`` line per set.

    Raises rather than skipping a line it cannot read: a silently dropped set is
    the failure mode this whole path exists to remove.
    """
    parsed = []
    for line in block.content.splitlines():
        if not line.strip():
            continue
        match = _MOVEMENT_LINE.match(line)
        # A trailing colon means a prose header slipped in ("3 rounds for quality:"),
        # which matches the shape but is not a movement. Caught here rather than at
        # the picker, where it would surface 30s later as a search timeout.
        if match and match.group(2).endswith(":"):
            match = None
        if not match:
            raise BTWBError(
                f"Block '{block.name}' is posted movement by movement, so every line must "
                f"read '<reps> <movement>'. Cannot parse: {line!r}"
            )
        parsed.append((match.group(2), match.group(1)))
    if not parsed:
        raise BTWBError(f"Block '{block.name}' has no movement lines to post")
    return parsed


def _remove_seed_movement(page: Page) -> None:
    """Delete the seed the AI textarea created; it is always the first row."""
    before = _movement_row_count(page)
    button = page.get_by_role("button", name=_DELETE_LABEL).first
    button.scroll_into_view_if_needed()
    # The row's action buttons sit under a hover overlay, so a plain click misses.
    button.click(force=True)
    page.wait_for_function(f"n => ({_ROW_COUNT_JS})(n) < n", arg=before, timeout=_TIMEOUT)


def _copy_movement_row(page: Page, name: str, reps: str) -> None:
    """Duplicate the row just added instead of searching for the movement again.

    BTWB's own COPIER inserts the copy directly beneath its source row, reps and
    all, so a run of identical sets keeps the order the block prescribes. Worth
    preferring: the movement search is the step that intermittently stalls for the
    full timeout, and a repeated set has no need of it. Guarded by the row count
    the same way as an add, so a late-landing copy is not retried into a duplicate.
    """
    for attempt in (1, 2):
        before = _movement_row_count(page)
        try:
            button = page.get_by_role("button", name=_COPY_LABEL).last
            button.scroll_into_view_if_needed()
            # Dispatched in the page rather than clicked: the row's actions sit under a
            # hover overlay, and as the list grows the last row slides under the sticky
            # footer, where even a forced click lands on the overlay instead.
            button.evaluate("el => el.click()")
            page.wait_for_function(f"n => ({_ROW_COUNT_JS})(n) > n", arg=before, timeout=_TIMEOUT)
            logger.info("  copied %s x%s", name, reps)
            return
        except PlaywrightTimeoutError:
            if _movement_row_count(page) > before:
                logger.info("  copied %s x%s (copy landed late)", name, reps)
                return
            if attempt == 2:
                raise
            logger.warning("  %s did not copy — retrying", name)


def _build_from_movements(page: Page, block: ProgrammingBlock) -> None:
    """Replace the seeded workout with this block's movements, entered exactly."""
    movements = _parse_movement_lines(block)
    searched = sum(1 for i, m in enumerate(movements) if i == 0 or movements[i - 1] != m)
    logger.info(
        "Entering %d movement(s) for '%s' — %d via search, %d copied",
        len(movements),
        block.name,
        searched,
        len(movements) - searched,
    )
    previous: tuple[str, str] | None = None
    for movement in movements:
        if movement == previous:
            _copy_movement_row(page, *movement)
        else:
            _add_movement(page, *movement)
        previous = movement
    _remove_seed_movement(page)


def _generate_preview(page: Page, description: str, block_name: str) -> None:
    """Submit *description* and wait for BTWB to render a preview of it.

    Retried once, which is safe precisely here: nothing has been planned yet, so
    the worst a second attempt costs is another parse.

    A retry does not rescue every failure. BTWB's generator refuses some
    prescriptions outright, returning no preview and no error however many times
    it is asked — holds written as times rather than reps ("Wall facing handstand
    Hold x 45 sec") are one shape it will not take. The caller reports those as
    blocks to add by hand rather than letting them vanish.
    """
    for attempt in (1, 2):
        description_field = page.locator(
            "textarea[name='planning_generated_workout[external_description]']"
        )
        description_field.wait_for(timeout=_TIMEOUT)
        description_field.fill(description)

        # Set up response listeners before the click so fast responses aren't missed
        with (
            page.expect_response(lambda r: "generated_workouts" in r.url, timeout=_TIMEOUT),
            page.expect_response(lambda r: "track_events" in r.url, timeout=_TIMEOUT),
        ):
            page.locator(
                "input[type='submit'][value='Continuer'], input[type='submit'][value='Continue']"
            ).first.click()

        # Wait for Planifier to become enabled (disabled while the preview loads)
        try:
            page.locator("button:has-text('Planifier'):not([disabled])").wait_for(
                state="visible", timeout=_TIMEOUT
            )
            return
        except PlaywrightTimeoutError:
            if attempt == 2:
                raise
            logger.warning("[%s] no preview after %ds — retrying", block_name, _TIMEOUT // 1000)
            page.reload(wait_until="domcontentloaded")


def _fill_and_plan(
    page: Page, block: ProgrammingBlock, last_block: bool, exact_movements: bool = False
) -> None:
    """Fill the workout description, submit, wait for preview, then click Planifier.

    With *exact_movements*, the textarea only seeds a workout to attach to and the
    real movements are entered through BTWB's movement search instead of its AI
    text parser. See the note above :data:`_SEED_DESCRIPTION`.
    """
    # Select the first track (Piste) if not already pre-selected by the URL
    track_select = page.locator("select[name='track_event[track_id]']")
    if track_select.count() and not track_select.input_value():
        track_select.select_option(index=1)
        page.wait_for_load_state("networkidle", timeout=_TIMEOUT)

    if block.erg is not None:
        plan_button = _fill_erg_intervals(page, block)
    elif block.sets is not None:
        plan_button = _fill_classic_sets(page, block)
    elif block.alternating is not None:
        plan_button = _fill_alternating_emom(page, block)
    else:
        _generate_preview(page, _SEED_DESCRIPTION if exact_movements else block.content, block.name)
        plan_button = page.locator("button:has-text('Planifier'):not([disabled])")

    if exact_movements:
        _build_from_movements(page, block)
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
    exact_movements: bool = False,
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
            _fill_and_plan(page, block, last_block=is_last, exact_movements=exact_movements)
            results.append({"block": block.name, "date": date_str, "ok": True})
            prev_saved = True
        except PlaywrightTimeoutError as e:
            # A timeout can now come from the movement picker as well as the AI
            # preview, so say which wait gave up: a skipped block is otherwise
            # only visible as a workout that quietly never appeared.
            logger.warning(
                "Block '%s' skipped — timed out on BTWB (%s)",
                block.name,
                str(e).splitlines()[0],
            )
            results.append({"block": block.name, "date": date_str, "skipped": True})
            prev_saved = False
        except BTWBError as e:
            # The classic builder refusing a block (an unknown movement, fields that did
            # not take) is about that block alone; the rest of the day still posts.
            logger.warning("Block '%s' skipped — %s", block.name, e)
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
      const titleEl = ev.querySelector('.title_track_event strong');
      const idMatch = action.match(/(\\d+)\\s*$/);
      out.push({
        date: matched,
        id: idMatch ? idMatch[1] : '',
        action,
        token: tokenEl ? tokenEl.value : '',
        title: titleEl ? (titleEl.getAttribute('title') || titleEl.textContent).trim() : '',
      });
    });
  });
  return out;
}
"""


def _collect_deletable_events(page: Page, dates: list[str]) -> list[dict]:
    """Return planned (deletable) workouts on the given ISO dates.

    Each entry is {date, id, action, token, title}. Loads the week view once per
    week the dates span; events are matched by an exact data-date hit, so a wrong
    or empty week simply yields nothing rather than touching unrelated days.

    The week view, not the month view: the month view leaves its day containers in
    the DOM without making them visible, so the wait below never resolves and
    delete reported nothing to delete however many workouts were planned.
    """
    events: list[dict] = []
    for _monday, week_dates in _group_dates_by_week(dates).items():
        # bring_to_front avoids macOS background-tab JS throttling (same reason as
        # _fetch_existing_block_names) while the calendar's AJAX content loads.
        page.bring_to_front()
        page.goto(_calendar_week_url(week_dates[0]), wait_until="domcontentloaded")
        # Select the track first, otherwise its workouts never render and the
        # scan finds nothing to delete.
        _ensure_track_selected(page)
        _settle_calendar(page)
        events.extend(page.evaluate(_SCAN_DELETABLE_JS, week_dates))
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


def _select_events(events: list[dict], titles: frozenset[str] | None) -> list[dict]:
    """The events to delete: all of them, or only those titled exactly as asked.

    An event whose title could not be read never matches a title filter, so a
    filtered delete can only ever remove what it names.
    """
    if titles is None:
        return events
    missing = titles - {e["title"] for e in events}
    if missing:
        logger.warning("Not planned on those dates: %s", ", ".join(sorted(missing)))
    return [e for e in events if e["title"] in titles]


def delete_week(
    email: str,
    password: str,
    dates: list[str],
    dry_run: bool = False,
    headless: bool = False,
    confirm: Callable[[list[dict]], bool] | None = None,
    titles: frozenset[str] | None = None,
) -> list[dict]:
    """Delete the planned workouts on the given ISO dates from BTWB.

    Logs in, scans the calendar for deletable workouts on ``dates`` — only those
    titled in *titles*, when given — optionally asks ``confirm`` to proceed, then
    deletes each one. With ``dry_run`` the workouts that would be deleted are
    logged and returned without deleting. Completed sessions (which have no
    delete option) are never touched.
    """
    results: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(locale="fr-FR")
        page = context.new_page()

        logger.info("Logging in...")
        _login(page, email, password)
        logger.info("Authenticated")

        events = _select_events(_collect_deletable_events(page, dates), titles)
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
    exact_movements: bool = False,
) -> list[dict]:
    """Post a week to BTWB.

    *exact_movements* is for blocks written as one ``"<reps> <movement>"`` line
    per set — accessory work, whose movement names BTWB's AI text parser resolves
    to the wrong exercise. Those are entered through BTWB's movement search
    instead. Leave it off for programming written as prose.
    """
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
            all_results.extend(
                _post_day(
                    page,
                    day,
                    dry_run=False,
                    existing=existing,
                    exact_movements=exact_movements,
                )
            )

        logger.info("All done — opening calendar")
        page.goto(f"{_BASE}/plan/calendar", wait_until="domcontentloaded")

        browser.close()

    return all_results
