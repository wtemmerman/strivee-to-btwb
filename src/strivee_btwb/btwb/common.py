"""What every BTWB page flow shares: the site, its waits, its errors, the session."""

import logging

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ..core import config

logger = logging.getLogger("btwb")


_BASE = "https://beyondthewhiteboard.com"


_TIMEOUT = 30_000  # ms — element/AJAX waits (incl. BTWB AI workout generation)


_CALENDAR_IDLE_TIMEOUT = 10_000  # ms — networkidle cap for calendar pages (non-fatal)


class BTWBError(Exception):
    pass


class AuthenticationError(BTWBError):
    pass


def _calendar_week_url(date_str: str) -> str:
    """Build the BTWB calendar-week URL for an ISO date, dropping zero-padding."""
    year, month, day = date_str.split("-")
    return f"{_BASE}/plan/calendar/week/{int(year)}/{int(month)}/{int(day)}"


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


_FIELD_COMMIT_MS = 500  # let the blur-driven units controller write the hidden input


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
