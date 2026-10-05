"""Re-capture the BTWB page fixtures the offline client tests run against.

    uv run python -m tests.fixtures.btwb.refresh

Logs in, opens each classic template on an empty date without saving (the
alternating EMOM with two movements entered, so its rows exist), and reads
three planned workouts of the week of 09-28 without changing them. Scripts,
tokens and the athlete's name are stripped before anything is written. Re-run it
when BTWB changes its markup and the offline tests start failing.
"""

import re
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from strivee_btwb.btwb.classic import _ALTERNATING_EMOM, _add_movement
from strivee_btwb.btwb.common import (
    _BASE,
    _calendar_week_url,
    _ensure_track_selected,
    _login,
    _settle_calendar,
)
from strivee_btwb.btwb.readback import _SCAN_EVENT_LINKS_JS
from strivee_btwb.core import config

OUT = Path(__file__).parent
EMPTY_DATE = "2026-10-04"
TEMPLATES = {
    "for_distance": ("Bike Erg", "for_distance"),
    "weightlifting_sets": ("Tempo Back Squat", "weightlifting_sets"),
    "gymnastics_sets": ("Strict Handstand Push-up", "gymnastics_sets"),
    "rep_max": ("Pause Squat Clean", "rep_max"),
    "single_emom": ("Butterfly Chest-to-bar Pull-up", "single_emom"),
}
ALTERNATING = (("Pause Squat Clean", "1"), ("Squat Clean", "1"))
EVENTS = {  # a lift in editor view, an erg with its RPE dropdown, a metcon summary
    "event_editor_lift": ("2026-09-28", "EMF 60 - Barbell Seal Row"),
    "event_editor_erg": ("2026-09-29", "EMF 60 - Bike erg"),
    "event_summary_metcon": ("2026-09-30", "EMF 60 - Mixed Modal Long Interval"),
}


def _scrubbed(html: str) -> str:
    html = re.sub(r"<script\b.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'(name="authenticity_token"[^>]*value=")[^"]*', r"\1TOKEN", html)
    html = re.sub(r'(<meta name="csrf-token" content=")[^"]*', r"\1TOKEN", html)
    html = re.sub(r"members/\d+", "members/0", html)
    html = re.sub(r"avatars/[\w-]+", "avatars/0", html)
    return html.replace("Wilfried TEMMERMAN", "Athlete").replace("wtemmerman", "athlete")


def _save(page: Page, name: str) -> None:
    (OUT / f"{name}.html").write_text(_scrubbed(page.content()))
    print("wrote", name)


def capture_alternating_emom(page: Page) -> None:
    page.goto(f"{_BASE}/plan/track_events/workouts/new?d={EMPTY_DATE}")
    page.locator(_ALTERNATING_EMOM).first.wait_for(state="visible")
    _save(page, "new_workout")
    page.locator(_ALTERNATING_EMOM).first.click()
    for name, reps in ALTERNATING:
        _add_movement(page, name, reps, percent_1rm=70)
    page.wait_for_timeout(1500)
    _save(page, "form_alternating_emom")


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_context(locale="fr-FR").new_page()
        _login(page, config.BTWB_EMAIL, config.BTWB_PASSWORD)
        for name, (movement, template) in TEMPLATES.items():
            page.goto(f"{_BASE}/plan/track_events/workouts/new?d={EMPTY_DATE}")
            page.locator("a[href='/plan/workouts/single']").first.click()
            search = page.locator("input#name")
            search.wait_for(state="visible")
            search.fill(movement)
            page.get_by_role("link", name=movement, exact=True).first.click()
            page.locator(f"a[href$='/{template}/new']").first.wait_for(state="visible")
            if name == "for_distance":
                _save(page, "template_chooser")
            page.locator(f"a[href$='/{template}/new']").first.click()
            page.locator("button[type='submit'][form='new_track_event']").first.wait_for()
            page.wait_for_timeout(1500)
            _save(page, f"form_{name}")
        capture_alternating_emom(page)
        for name, (day, title) in EVENTS.items():
            page.goto(_calendar_week_url(day), wait_until="domcontentloaded")
            _ensure_track_selected(page)
            _settle_calendar(page)
            events = page.evaluate(_SCAN_EVENT_LINKS_JS, [day])
            href = next(e["href"] for e in events if e["title"] == title)
            page.goto(_BASE + href, wait_until="networkidle")
            _save(page, name)
        browser.close()


if __name__ == "__main__":
    main()
