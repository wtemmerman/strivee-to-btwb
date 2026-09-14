"""Read completed activities from Garmin Connect.

There is no public Garmin API for a personal account, so this goes through the
same endpoints the Connect app uses. Two consequences shape the module.

Auth is token-based. ``login`` is run by hand once and writes refresh tokens
outside the repo; every sync loads those, so the password is never stored and
never read from the environment. Tokens last about a year, and renewing them can
demand an MFA code — which is why re-login is a person's job, not the sync's.

Garmin rate-limits by IP, hard: a login burst answers 429 before it answers
anything else. Every call is therefore retried with a widening wait, and the
final attempt is left to raise. A sync that dies loudly on a 429 is recoverable;
one that returns an empty week silently is not.
"""

import logging
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from garminconnect import Garmin, GarminConnectTooManyRequestsError

from ..core import config

logger = logging.getLogger("garmin")


class GarminError(Exception):
    pass


class GarminAuthError(GarminError):
    pass


_RETRY_WAITS = (5, 20, 60)
"""Seconds to wait before each retry of a rate-limited call.

Garmin does not say how long its window is, so this widens rather than polls:
three waits cover a burst limit, and anything longer than that is an account- or
IP-level block that waiting will not clear."""


def _with_backoff(call: Callable[[], Any], what: str) -> Any:
    for wait in _RETRY_WAITS:
        try:
            return call()
        except GarminConnectTooManyRequestsError:
            logger.warning("Garmin rate-limited %s — retrying in %ds", what, wait)
            time.sleep(wait)
    return call()


def connect(tokenstore: str | None = None) -> Garmin:
    """Open a Garmin session from stored tokens."""
    store = tokenstore or config.GARMIN_TOKENSTORE
    if not Path(store).exists():
        raise GarminAuthError(
            f"No Garmin tokens at {store} — run 'strivee-btwb garmin-login' first."
        )
    client = Garmin()
    client.login(store)
    logger.info("Garmin: signed in as %s", client.get_full_name())
    return client


def login(email: str, password: str, prompt_mfa: Callable[[], str]) -> Garmin:
    """Sign in with credentials and persist the tokens. Interactive: MFA may prompt."""
    client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
    client.login(config.GARMIN_TOKENSTORE)
    logger.info("Garmin tokens written to %s", config.GARMIN_TOKENSTORE)
    return client


def fetch_activities(client: Garmin, start: date, end: date) -> list[dict]:
    """Return every activity Garmin holds between *start* and *end*, inclusive."""
    activities = _with_backoff(
        lambda: client.get_activities_by_date(start.isoformat(), end.isoformat()),
        f"activities {start}..{end}",
    )
    logger.info("Garmin returned %d activities for %s → %s", len(activities), start, end)
    return activities


def fetch_laps(client: Garmin, activity_id: int) -> list[dict]:
    """Return one activity's lap records — where a structured workout's reps live."""
    splits = _with_backoff(
        lambda: client.get_activity_splits(activity_id), f"laps for activity {activity_id}"
    )
    return splits.get("lapDTOs", [])


def fetch_with_laps(
    client: Garmin, start: date, end: date, needs_laps: Callable[[dict], bool]
) -> list[dict]:
    """Fetch a date range, attaching lap records to the activities that need them.

    Laps cost one request each, so the caller decides which activities are worth
    it — only a run can hide an interval structure that changes how it is logged.
    """
    activities = fetch_activities(client, start, end)
    enriched = []
    for activity in activities:
        if not needs_laps(activity):
            enriched.append(activity)
            continue
        laps = fetch_laps(client, activity["activityId"])
        enriched.append({**activity, "lapDTOs": laps})
    return enriched
