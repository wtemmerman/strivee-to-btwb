"""Turn Garmin activities into the cardio sessions BTWB will log.

Two judgements live here, and nowhere else.

The first is which BTWB scoring model a session belongs to. Garmin labels every
lap of a structured workout with an intensity — WARMUP, ACTIVE, REST, RECOVERY —
so a rep session is recognisable from the lap sequence alone: repeated ACTIVE
efforts with REST between them. That sequence is the signal, not Garmin's
``hasIntensityIntervals`` flag, which is also true of a Zone 2 run whose only
structure is a warm-up block. Such a run is one continuous effort as far as BTWB
is concerned, and logging it as intervals would invent reps that were never run.

The second is what counts as one entry. Short rides repeat twice a day — out to
the box and home again — and they are travel, not training. One BTWB entry per
leg buries the rides that were training, so below MIN_BIKE_KM a day's rides fold
into a single entry that keeps the volume without the clutter.

Everything here is pure: Garmin dicts in, CardioSession out. The network lives in
:mod:`..garmin.client`, and what BTWB's form does with a session lives in the
writer — so the mapping can be re-run over a cached week as often as it takes.
"""

import logging
from collections import defaultdict
from datetime import date

from ..core import config
from ..core.models import INTERVALS, SINGLE_DISTANCE, CardioInterval, CardioSession

logger = logging.getLogger("processing")

MOVEMENT_RUN = "Run"
MOVEMENT_ROAD_BIKE = "Road Bike"

RUN_TYPES = frozenset({"running", "track_running", "trail_running"})
RIDE_TYPES = frozenset({"cycling", "road_biking", "gravel_cycling"})

MOVEMENT_BY_TYPE: dict[str, str] = {
    **dict.fromkeys(RUN_TYPES, MOVEMENT_RUN),
    **dict.fromkeys(RIDE_TYPES, MOVEMENT_ROAD_BIKE),
}
"""Garmin activity type → the BTWB movement it is logged as.

Deliberately narrow. Walking, HIIT and indoor cardio are either already logged in
BTWB as CrossFit or are not training this is meant to track; an unmapped type is
skipped and said out loud rather than guessed at.
"""

COMMUTE_TITLE = "Bike Commute"
"""Title every merged short-ride entry carries, on every date.

Constant on purpose, like the accessory block: a title that counted the legs
would change when a third ride appeared, and the day would gain a second entry
beside the stale one instead of updating.
"""

ACTIVE = "ACTIVE"
REST = "REST"

UNIFORM_TOLERANCE_M = 10.0
"""How far rep distances may spread and still count as "same each interval".

A structured workout reports its reps as exactly 500m; a hand-lapped rep drifts a
few metres. Ten metres keeps 5 x 500m together without collapsing a 400/500 ladder
into one number."""

_ACTIVITY_URL = "https://connect.garmin.com/modern/activity/{}"


# ── Reading the Garmin shape ──────────────────────────────────────────────────


def type_key(activity: dict) -> str:
    return (activity.get("activityType") or {}).get("typeKey", "")


def movement_for(activity: dict) -> str | None:
    """The BTWB movement this activity is logged as, or None when it is not synced."""
    return MOVEMENT_BY_TYPE.get(type_key(activity))


def needs_laps(activity: dict) -> bool:
    """Whether this activity's laps are worth a request — only a run hides reps."""
    return type_key(activity) in RUN_TYPES


def activity_date(activity: dict) -> date:
    """The local calendar day the activity started, which is the day BTWB logs it on."""
    return date.fromisoformat(activity["startTimeLocal"][:10])


def _number(activity: dict, field: str) -> float:
    return float(activity.get(field) or 0.0)


# ── Interval structure ────────────────────────────────────────────────────────


def is_interval_session(laps: list[dict]) -> bool:
    """True when the laps read as repeated efforts with rest, not one continuous run."""
    intensities = [lap.get("intensityType") for lap in laps]
    return intensities.count(ACTIVE) >= 2 and REST in intensities


def work_intervals(laps: list[dict]) -> list[CardioInterval]:
    """The work efforts of a rep session, each carrying the rest that followed it."""
    intervals = []
    for index, lap in enumerate(laps):
        if lap.get("intensityType") != ACTIVE:
            continue
        following = laps[index + 1] if index + 1 < len(laps) else None
        rest = (
            float(following["duration"])
            if following and following.get("intensityType") == REST
            else None
        )
        intervals.append(
            CardioInterval(
                distance_m=float(lap.get("distance") or 0.0),
                duration_s=float(lap.get("duration") or 0.0),
                rest_s=rest,
            )
        )
    return intervals


def same_each_interval(intervals: list[CardioInterval]) -> bool:
    """Whether every rep covered the same distance, within watch accuracy."""
    distances = [i.distance_m for i in intervals]
    return max(distances) - min(distances) <= UNIFORM_TOLERANCE_M


# ── Notes ─────────────────────────────────────────────────────────────────────


def clock(seconds: float) -> str:
    """Format a duration the way a training log reads it: 51:07, or 1:02:30."""
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def pace_per_km(distance_m: float, duration_s: float) -> str:
    return f"{clock(duration_s / (distance_m / 1000))} /km"


def speed_kmh(distance_m: float, duration_s: float) -> str:
    return f"{distance_m / 1000 / (duration_s / 3600):.1f} km/h"


def _effort_note(movement: str, distance_m: float, duration_s: float) -> str | None:
    """The pace line, in the unit that sport is read in — per-km running, km/h riding."""
    if distance_m <= 0 or duration_s <= 0:
        return None
    if movement == MOVEMENT_RUN:
        return f"Avg pace {pace_per_km(distance_m, duration_s)}"
    return f"Avg speed {speed_kmh(distance_m, duration_s)}"


def _notes(activity: dict, movement: str) -> str:
    # The logger names the entry itself ("Run, 8.99 km"), so the name KipRun gave
    # the session — which is the prescription — survives only if the notes carry it.
    lines = [activity["activityName"]] if activity.get("activityName") else []
    avg_hr, max_hr = _number(activity, "averageHR"), _number(activity, "maxHR")
    if avg_hr:
        lines.append(f"Avg HR {avg_hr:.0f} bpm · Max {max_hr:.0f} bpm")
    effort = _effort_note(movement, _number(activity, "distance"), _number(activity, "duration"))
    if effort:
        lines.append(effort)
    gain = _number(activity, "elevationGain")
    if gain:
        lines.append(f"Elevation gain {gain:.0f} m")
    lines.append(_ACTIVITY_URL.format(activity["activityId"]))
    return "\n".join(lines)


# ── Sessions ──────────────────────────────────────────────────────────────────


def _session(activity: dict, movement: str) -> CardioSession:
    laps = activity.get("lapDTOs") or []
    intervals = work_intervals(laps) if is_interval_session(laps) else []
    return CardioSession(
        date=activity_date(activity),
        movement=movement,
        model=INTERVALS if intervals else SINGLE_DISTANCE,
        title=activity.get("activityName") or movement,
        distance_m=_number(activity, "distance"),
        duration_s=_number(activity, "duration"),
        intervals=intervals,
        notes=_notes(activity, movement),
        source_ids=(activity["activityId"],),
    )


def _merged_commute(activities: list[dict]) -> CardioSession:
    """Fold one day's short rides into the single entry that stands for the travel."""
    distance = sum(_number(a, "distance") for a in activities)
    duration = sum(_number(a, "duration") for a in activities)
    # Weighted by time on the bike: a 20-minute leg says more about the day's
    # average heart rate than a 12-minute one.
    hr_time = sum(_number(a, "duration") for a in activities if _number(a, "averageHR"))
    avg_hr = (
        sum(_number(a, "averageHR") * _number(a, "duration") for a in activities) / hr_time
        if hr_time
        else 0.0
    )
    lines = []
    if avg_hr:
        lines.append(
            f"Avg HR {avg_hr:.0f} bpm · Max {max(_number(a, 'maxHR') for a in activities):.0f} bpm"
        )
    effort = _effort_note(MOVEMENT_ROAD_BIKE, distance, duration)
    if effort:
        lines.append(effort)
    gain = sum(_number(a, "elevationGain") for a in activities)
    if gain:
        lines.append(f"Elevation gain {gain:.0f} m")
    lines.append(f"{len(activities)} rides merged:")
    lines += [
        f"  {_number(a, 'distance') / 1000:.2f} km in {clock(_number(a, 'duration'))}"
        f"  {_ACTIVITY_URL.format(a['activityId'])}"
        for a in activities
    ]
    return CardioSession(
        date=activity_date(activities[0]),
        movement=MOVEMENT_ROAD_BIKE,
        model=SINGLE_DISTANCE,
        title=COMMUTE_TITLE,
        distance_m=distance,
        duration_s=duration,
        notes="\n".join(lines),
        source_ids=tuple(a["activityId"] for a in activities),
    )


def sessions_from_activities(
    activities: list[dict], min_bike_km: float | None = None
) -> list[CardioSession]:
    """Map a fetched range of Garmin activities to the BTWB entries they become."""
    threshold_m = (config.MIN_BIKE_KM if min_bike_km is None else min_bike_km) * 1000
    sessions: list[CardioSession] = []
    commutes: dict[date, list[dict]] = defaultdict(list)

    for activity in sorted(activities, key=lambda a: a["startTimeLocal"]):
        movement = movement_for(activity)
        if movement is None:
            logger.info(
                "Not synced: %s on %s (%s)",
                activity.get("activityName"),
                activity_date(activity),
                type_key(activity) or "no activity type",
            )
            continue
        if movement == MOVEMENT_ROAD_BIKE and _number(activity, "distance") < threshold_m:
            commutes[activity_date(activity)].append(activity)
            continue
        sessions.append(_session(activity, movement))

    sessions += [_merged_commute(day) for day in commutes.values()]
    return sorted(sessions, key=lambda s: (s.date, s.title))
