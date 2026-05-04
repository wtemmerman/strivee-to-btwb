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
       e. For subsequent blocks: click "+" → "Nouvel entraînement" from dropdown
  4. After all days: navigate to /plan/calendar
"""

import logging

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..core import config
from ..core.models import DayProgramming, ProgrammingBlock, WeeklyProgramming

logger = logging.getLogger("btwb")

_BASE = "https://beyondthewhiteboard.com"
_TIMEOUT = 30_000  # ms


class BTWBError(Exception):
    pass


class AuthenticationError(BTWBError):
    pass


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
        page.wait_for_load_state("networkidle", timeout=_TIMEOUT)

    logger.info("Block '%s' saved", block.name)


def _fetch_existing_block_names(page: Page, date_str: str) -> set[str]:
    """Return workout titles already planned for this date on BTWB."""
    # bring_to_front prevents macOS background-tab JS throttling when the
    # terminal has focus (e.g. user just typed at the confirmation prompt)
    page.bring_to_front()
    year, month, day = date_str.split("-")
    page.goto(
        f"{_BASE}/plan/calendar/week/{int(year)}/{int(month)}/{int(day)}",
        wait_until="domcontentloaded",
    )
    # Ensure the personal track checkbox is checked so workouts appear on the calendar.
    # wait_for(attached) is required — the checkbox is injected by JS after domcontentloaded,
    # so count() would return 0 and the click would be silently skipped without this wait.
    if config.BTWB_TRACK_ID:
        track_cb = page.locator(f"#plan_track_{config.BTWB_TRACK_ID}")
        track_cb.wait_for(state="attached", timeout=_TIMEOUT)
        if not track_cb.is_checked():
            track_cb.click()
    # Wait for the calendar day containers to be injected by JS, then wait for
    # networkidle so any AJAX workout loads inside those containers also finish.
    page.wait_for_selector("[data-date]", timeout=_TIMEOUT)
    page.wait_for_load_state("networkidle", timeout=_TIMEOUT)
    # Find workouts for this specific date via the data-date container
    titles: list[str] = page.evaluate(f"""
        () => {{
            const results = [];
            // data-date attribute value includes the date string (possibly with extra quotes)
            let root = null;
            document.querySelectorAll('[data-date]').forEach(el => {{
                if (el.getAttribute('data-date').includes('{date_str}')) root = el;
            }});
            if (!root) return results;
            root.querySelectorAll('.title_track_event strong').forEach(el => {{
                const t = (el.getAttribute('title') || el.textContent || '').trim();
                if (t) results.push(t);
            }});
            return results;
        }}
    """)
    logger.info("Existing blocks on BTWB for %s: %s", date_str, titles or "none")
    return set(titles)


def _post_day(page: Page, day: DayProgramming, dry_run: bool) -> list[dict]:
    date_str = day.date.isoformat()
    logger.info("%s %s — %d block(s)", day.day_label, date_str, len(day.blocks))
    results = []

    if not dry_run:
        existing = _fetch_existing_block_names(page, date_str)
        if existing:
            logger.info("Already on BTWB: %s", ", ".join(sorted(existing)))
    else:
        existing = set()

    blocks_to_post = [b for b in day.blocks if b.name not in existing]
    if not blocks_to_post:
        logger.info("%s — all blocks already posted, skipping", day.day_label)
        return results

    for i, block in enumerate(blocks_to_post):
        if dry_run:
            logger.info("[dry-run] Would submit '%s': %s...", block.name, block.content[:60])
            results.append({"dry_run": True, "block": block.name, "date": date_str})
            continue

        logger.info("Submitting block '%s'", block.name)
        is_last = i == len(blocks_to_post) - 1

        if i == 0:
            # First block: navigate to the new workout form
            page.goto(
                f"{_BASE}/plan/track_events/workouts/new?d={date_str}",
                wait_until="domcontentloaded",
            )
        else:
            # Subsequent blocks: open "+" dropdown, read the href, navigate directly
            plus_button = page.locator(
                "button.btn-outline-grey-200[data-bs-toggle='dropdown']:not([disabled])"
            ).first
            plus_button.click()
            new_link = page.locator("a.dropdown-item:has-text('Nouvel entraînement')")
            new_link.wait_for(state="attached", timeout=_TIMEOUT)
            href = new_link.get_attribute("href")
            page.goto(f"{_BASE}{href}", wait_until="domcontentloaded")

        try:
            _fill_and_plan(page, block, last_block=is_last)
            results.append({"block": block.name, "date": date_str, "ok": True})
        except PlaywrightTimeoutError:
            logger.warning("Block '%s' skipped — BTWB AI did not generate a preview", block.name)
            results.append({"block": block.name, "date": date_str, "skipped": True})

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

        for day in days_to_post:
            all_results.extend(_post_day(page, day, dry_run=False))

        logger.info("All done — opening calendar")
        page.goto(f"{_BASE}/plan/calendar", wait_until="domcontentloaded")

        browser.close()

    return all_results
