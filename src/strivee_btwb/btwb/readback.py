"""Reading back what BTWB holds: planned workouts, logged loads, completed titles."""

import logging

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .common import (
    _BASE,
    _CALENDAR_IDLE_TIMEOUT,
    _calendar_week_url,
    _ensure_track_selected,
    _login,
    _settle_calendar,
)

logger = logging.getLogger("btwb")


# A completed event carries a check badge inside its title row, and BTWB also
# nests it under .track-event-event-results. Both were verified to agree on every
# event of a real week; the badge is used because it is the narrower claim.
_SCAN_COMPLETED_JS = """
(dates) => {
  const out = {};
  dates.forEach(d => { out[d] = []; });
  document.querySelectorAll('[data-date]').forEach(el => {
    const raw = el.getAttribute('data-date') || '';
    const d = dates.find(x => raw.includes(x));
    if (!d) return;
    el.querySelectorAll('.title_track_event').forEach(node => {
      if (!node.querySelector('.badge-track-orange .mdi-check')) return;
      const s = node.querySelector('strong');
      const t = ((s && (s.getAttribute('title') || s.textContent)) || '').trim();
      if (t) out[d].push(t);
    });
  });
  return out;
}
"""


# The week view carries only a one-line summary per event, and truncates it
# ("...and 7 more"), so it cannot answer what BTWB stored. This collects the link
# to each event instead; the body is read from the event page below.
_SCAN_EVENT_LINKS_JS = """
(dates) => {
  const out = [];
  document.querySelectorAll('[data-date]').forEach(el => {
    const raw = el.getAttribute('data-date') || '';
    const d = dates.find(x => raw.includes(x));
    if (!d) return;
    el.querySelectorAll('.calendar_track_event').forEach(ev => {
      const s = ev.querySelector('.title_track_event strong');
      const a = ev.querySelector("a[href*='/plan/track_events/']");
      const t = s ? (s.getAttribute('title') || s.textContent).trim() : '';
      if (t && a) out.push({date: d, title: t, href: a.getAttribute('href')});
    });
  });
  return out;
}
"""

# On the event page the workout tab holds the full movement list. Everything from
# the edit buttons down is chrome; lines ending in a full stop are BTWB's own
# canned description of the movement, not part of the prescription.
_STORED_BODY_JS = """
() => {
  const root = document.querySelector('#workout');
  if (!root) return '';
  const frame = root.querySelector('turbo-frame.edit_workout') || root;
  const lines = (frame.innerText || '').split('\\n').map(l => l.trim()).filter(Boolean);
  // A saved workout ends at "MODIFIER L'ENTRAÎNEMENT"; one still in the editor
  // ends at "PLANIFIER L'ENTRAÎNEMENT". Cut at whichever comes first.
  const ends = ["MODIFIER L'ENTRA", "PLANIFIER L'ENTRA", "METTRE À JOUR"];
  const end = lines.findIndex(l => ends.some(e => l.startsWith(e)));
  const rowActions = ['MODIFIER', 'COPIER', 'SUPPRIMER', 'AJOUTER MOUVEMENT',
                      'AJOUTER COMPLEXE', 'AJOUTER REPOS'];
  // Lift workouts (X Rep Max, weightlifting sets) render as the editor, their
  // values inside inputs that innerText cannot see. Each row is written out as
  // "<reps> <movement>", the shape the stored-vs-posted check reads.
  const names = [...frame.querySelectorAll("input[name='definition[contents][][movementName]']")];
  const reps = [...frame.querySelectorAll("input[name='definition[contents][][reps][value]']")];
  const after = (a, b) => Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  const rows = names.map((name, i) => {
    const next = names[i + 1];
    const count = reps.find(r => after(name, r) && (!next || after(r, next)));
    return `${(count && count.value) || 1} ${name.value}`;
  });
  // A dropdown's options are part of innerText: the erg editor's RPE scale would
  // read as "19 - 100% effort" workout lines.
  const options = new Set([...frame.querySelectorAll('option')].map(o => o.textContent.trim()));
  return (end === -1 ? lines : lines.slice(0, end))
    .filter(l => !rowActions.includes(l) && !options.has(l))
    .filter(l => !l.endsWith('.') && !l.endsWith(':'))
    .concat(rows)
    .join('\\n');
}
"""


_SCAN_SESSION_LINKS_JS = """
(dates) => {
  const out = [];
  document.querySelectorAll('[data-date]').forEach(el => {
    const raw = el.getAttribute('data-date') || '';
    const d = dates.find(x => raw.includes(x));
    if (!d) return;
    el.querySelectorAll('.calendar_track_event').forEach(ev => {
      if (!ev.querySelector('.badge-track-orange .mdi-check')) return;
      const s = ev.querySelector('.title_track_event strong');
      const a = [...ev.querySelectorAll('a')]
        .map(x => x.getAttribute('href'))
        .find(h => h && h.includes('workout_sessions'));
      const t = s ? (s.getAttribute('title') || s.textContent).trim() : '';
      if (t && a) out.push({date: d, title: t, href: a});
    });
  });
  return out;
}
"""

# A logged session lists the prescription rows, then RÉSULTAT, then the result.
_SESSION_RESULT_JS = """
() => {
  const t = (document.body.innerText || '').split('\\n').map(l => l.trim()).filter(Boolean);
  const i = t.indexOf('RÉSULTAT');
  if (i === -1) return {rows: [], result: ''};
  return {rows: t.slice(0, i), result: t[i + 1] || ''};
}
"""


def fetch_logged_loads(
    email: str,
    password: str,
    dates: list[str],
    headless: bool = True,
) -> dict[str, dict[str, tuple[list[str], str]]]:
    """Return {date: {title: (prescription rows, result line)}} for logged sessions.

    Only sessions marked done have a result to read. Rows come back unfiltered;
    the caller decides which of them are prescriptions, using the same
    leading-count rule the posted-vs-stored check uses.
    """
    if not dates:
        return {}
    logged: dict[str, dict[str, tuple[list[str], str]]] = {d: {} for d in dates}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_context(locale="fr-FR").new_page()
        try:
            _login(page, email, password)
            page.goto(_calendar_week_url(dates[0]), wait_until="domcontentloaded")
            _ensure_track_selected(page)
            _settle_calendar(page)
            sessions = page.evaluate(_SCAN_SESSION_LINKS_JS, dates)
            logger.info("Reading %d logged session(s) from BTWB", len(sessions))
            for session in sessions:
                page.goto(_BASE + session["href"], wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=_CALENDAR_IDLE_TIMEOUT)
                except PlaywrightTimeoutError:
                    pass
                found = page.evaluate(_SESSION_RESULT_JS)
                logged[session["date"]][session["title"]] = (found["rows"], found["result"])
        finally:
            browser.close()
    return logged


def fetch_planned_workouts(
    email: str,
    password: str,
    dates: list[str],
    headless: bool = True,
) -> dict[str, dict[str, str]]:
    """Return {date: {title: the workout as BTWB stored it}} for *dates*.

    One calendar load to find the events, then one page per event: the week view
    only summarises a workout and truncates the summary, so it cannot say what
    BTWB actually stored.

    Like the duplicate check, this assumes *dates* fall in one week.
    """
    if not dates:
        return {}
    stored: dict[str, dict[str, str]] = {d: {} for d in dates}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_context(locale="fr-FR").new_page()
        try:
            _login(page, email, password)
            page.goto(_calendar_week_url(dates[0]), wait_until="domcontentloaded")
            _ensure_track_selected(page)
            _settle_calendar(page)
            events = page.evaluate(_SCAN_EVENT_LINKS_JS, dates)
            logger.info("Reading %d planned workout(s) back from BTWB", len(events))
            for event in events:
                page.goto(_BASE + event["href"], wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=_CALENDAR_IDLE_TIMEOUT)
                except PlaywrightTimeoutError:
                    pass
                stored[event["date"]][event["title"]] = page.evaluate(_STORED_BODY_JS)
        finally:
            browser.close()
    return stored


def fetch_completed_titles(
    email: str,
    password: str,
    dates: list[str],
    headless: bool = True,
) -> dict[str, set[str]]:
    """Return {date: titles of blocks logged as done} for *dates*.

    Reads the same week view the duplicate check already loads, so this is one
    page load rather than a new way into BTWB. Only completion is read — nothing
    about what was actually lifted — which is all the audit needs to stop
    counting a session that was planned and skipped.

    Like the duplicate check, this assumes *dates* fall in one week and loads the
    week containing the first of them.
    """
    if not dates:
        return {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_context(locale="fr-FR").new_page()
        try:
            _login(page, email, password)
            page.goto(_calendar_week_url(dates[0]), wait_until="domcontentloaded")
            _ensure_track_selected(page)
            _settle_calendar(page)
            done: dict[str, list[str]] = page.evaluate(_SCAN_COMPLETED_JS, dates)
        finally:
            browser.close()
    return {d: set(done.get(d, [])) for d in dates}
