"""Log the runs and rides Garmin recorded into BTWB, skipping what is already there."""

import getpass
import logging
import sys
from datetime import date, timedelta

from .btwb import (
    sync_sessions,
)
from .core import config
from .garmin import GarminAuthError, fetch_with_laps
from .garmin import connect as garmin_connect
from .garmin import load_week as load_garmin_week
from .garmin import login as garmin_login
from .garmin import save_week as save_garmin_week
from .pipeline import week_start
from .processing.garmin_map import activity_date, needs_laps, sessions_from_activities
from .processing.timing import clock

logger = logging.getLogger(__name__)

# ── Garmin → BTWB cardio sync ─────────────────────────────────────────────────

GARMIN_DEFAULT_DAYS_BACK = 7
"""Days of Garmin history a sync looks at when no week is named — one training week."""


def _garmin_window(days_back: int, ws: date | None) -> tuple[date, date]:
    """The range to sync: a whole week when one is named, else the last N days."""
    if ws:
        return ws, ws + timedelta(days=6)
    end = date.today()
    return end - timedelta(days=days_back - 1), end


def _mondays_between(start: date, end: date) -> list[date]:
    first = week_start(start)
    return [first + timedelta(weeks=n) for n in range((week_start(end) - first).days // 7 + 1)]


def _garmin_activities(start: date, end: date, refetch: bool) -> list[dict]:
    """Fetch the window from Garmin a week at a time, reusing finished weeks.

    Garmin is rate-limited, so a week fetched after it ended is read from disk
    instead of asked for again; anything else is fetched (see `load_week`).
    """
    client = None
    activities: list[dict] = []
    for monday in _mondays_between(start, end):
        sunday = monday + timedelta(days=6)
        cached = None if refetch else load_garmin_week(monday)
        if cached is not None:
            logger.info("Week of %s — %d activities from cache", monday, len(cached))
            activities += cached
            continue
        client = client or garmin_connect()
        fetched = fetch_with_laps(client, monday, sunday, needs_laps)
        save_garmin_week(monday, fetched)
        activities += fetched
    return [a for a in activities if start <= activity_date(a) <= end]


def _report_cardio(outcome: dict, post: bool) -> None:
    logged, skipped = outcome["logged"], outcome["skipped"]
    for entry in skipped:
        logger.info("  already on BTWB — %s  %s", entry["date"], entry["title"])
    verb = "logged" if post else "would log"
    for entry in logged:
        logger.info("  %s — %s  %s | %s", verb, entry["date"], entry["shape"], entry["result"])
        if entry.get("url"):
            logger.info("      %s", entry["url"])
    logger.info("")
    if not logged:
        logger.info("  Nothing new — BTWB already holds every session in the window.")
    elif post:
        logger.info("  %d session(s) logged, %d already there.", len(logged), len(skipped))
    else:
        logger.info(
            "  %d session(s) would be logged, %d already there. Re-run with --post to write them.",
            len(logged),
            len(skipped),
        )


def do_garmin(
    days_back: int = GARMIN_DEFAULT_DAYS_BACK,
    ws: date | None = None,
    min_bike_km: float | None = None,
    post: bool = False,
    yes: bool = False,
    headless: bool = False,
    refetch: bool = False,
    no_commutes: bool = False,
) -> None:
    """Log the runs and rides Garmin recorded into BTWB, skipping what is already there."""
    if not config.BTWB_EMAIL or not config.BTWB_PASSWORD:
        logger.error("BTWB_EMAIL and BTWB_PASSWORD must be set in .env")
        sys.exit(1)
    start, end = _garmin_window(days_back, ws)
    logger.info("=" * 68)
    logger.info("  Garmin → BTWB — %s to %s", start, end)
    logger.info("=" * 68)

    try:
        activities = _garmin_activities(start, end, refetch)
    except GarminAuthError as e:
        logger.error("%s", e)
        sys.exit(1)

    sessions = sessions_from_activities(activities, min_bike_km, merge_commutes=not no_commutes)
    if not sessions:
        logger.info("  No runs or rides in the window.")
        return
    for session in sessions:
        logger.info(
            "  %s  %-10s %7.2f km  %8s  %s",
            session.date,
            session.movement,
            session.distance_m / 1000,
            clock(session.duration_s),
            session.title,
        )
    logger.info("")

    if post and not yes and not _confirm_cardio(sessions):
        logger.info("Aborted — nothing written.")
        return

    try:
        outcome = sync_sessions(sessions, start, dry_run=not post, headless=headless)
    except Exception as e:
        logger.error("Sync failed: %s", e)
        sys.exit(1)
    _report_cardio(outcome, post)


def _confirm_cardio(sessions: list) -> bool:
    answer = input(
        f"\nLog up to {len(sessions)} session(s) to BTWB? Ones already there are skipped. [y/N] "
    )
    return answer.strip().lower() in ("y", "yes")


def do_garmin_login() -> None:
    """Sign in to Garmin once, so every later sync reads tokens instead of a password."""
    email = input("Garmin Connect email: ").strip()
    password = getpass.getpass("Garmin Connect password: ")
    try:
        garmin_login(email, password, lambda: input("MFA code: ").strip())
    except Exception as e:
        logger.error("Garmin login failed: %s", e)
        sys.exit(1)
    logger.info("Tokens written to %s — the sync will not ask again.", config.GARMIN_TOKENSTORE)
