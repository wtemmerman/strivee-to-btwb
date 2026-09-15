"""Unit tests for the Garmin → BTWB mapping.

Every activity here is hand-written to the shape Garmin actually returns (metres,
seconds, m/s, `intensityType` per lap), so a failure means a mapping rule
changed, never that Garmin had a bad day.
"""

from datetime import date

import pytest

from strivee_btwb.core.models import INTERVALS, SINGLE_DISTANCE, CardioInterval
from strivee_btwb.processing.garmin_map import (
    COMMUTE_TITLE,
    MOVEMENT_ROAD_BIKE,
    MOVEMENT_RUN,
    clock,
    is_interval_session,
    needs_laps,
    same_each_interval,
    sessions_from_activities,
)


def _lap(intensity, distance, duration):
    return {"intensityType": intensity, "distance": distance, "duration": duration}


def _activity(
    type_key="running",
    *,
    activity_id=1,
    name="La Prairie Course à pied",
    start="2026-09-13 08:04:29",
    km=10.0,
    secs=3600.0,
    avg_hr=None,
    max_hr=None,
    gain=None,
    laps=None,
    **extra,
):
    activity = {
        "activityId": activity_id,
        "activityName": name,
        "activityType": {"typeKey": type_key},
        "startTimeLocal": start,
        "distance": km * 1000,
        "duration": secs,
        "averageHR": avg_hr,
        "maxHR": max_hr,
        "elevationGain": gain,
        **extra,
    }
    if laps is not None:
        activity["lapDTOs"] = laps
    return activity


# Five 500m efforts with 200m jog recoveries, as the watch recorded them.
REP_LAPS = [
    _lap("WARMUP", 1000, 369.7),
    _lap("WARMUP", 1000, 372.1),
    _lap("ACTIVE", 500, 118.0),
    _lap("REST", 200, 80.4),
    _lap("ACTIVE", 500, 122.6),
    _lap("REST", 200, 77.1),
    _lap("ACTIVE", 500, 115.8),
    _lap("REST", 200, 76.6),
    _lap("ACTIVE", 500, 119.1),
    _lap("REST", 200, 84.6),
    _lap("ACTIVE", 500, 121.1),
    _lap("REST", 200, 89.1),
    _lap("RECOVERY", 1000, 386.5),
]

# A structured but continuous run: auto-lap per km, every lap flagged ACTIVE.
STEADY_LAPS = [_lap("ACTIVE", 1000, 353.1) for _ in range(6)]

# A free run: no structure at all, so Garmin labels no intensity.
FREE_LAPS = [_lap(None, 1000, 360.0) for _ in range(10)]


# ── Choosing the BTWB model ───────────────────────────────────────────────────


def test_repeated_efforts_with_rest_are_intervals():
    assert is_interval_session(REP_LAPS)


def test_a_structured_steady_run_is_a_single_distance():
    """Every lap ACTIVE means auto-lap, not reps — there is no rest to separate them."""
    assert not is_interval_session(STEADY_LAPS)


# A ladder whose recoveries were jogged: every lap ACTIVE, none of them an auto-lap.
LADDER_LAPS = [
    _lap("WARMUP", 1000, 378.3),
    _lap("ACTIVE", 100, 28.2),
    _lap("ACTIVE", 100, 37.1),
    _lap("ACTIVE", 200, 47.1),
    _lap("ACTIVE", 100, 42.1),
    _lap("ACTIVE", 300, 70.4),
    _lap("ACTIVE", 100, 38.4),
    _lap("ACTIVE", 400, 99.0),
    _lap("RECOVERY", 626, 240.6),
]


def test_a_ladder_with_jogged_recoveries_is_still_intervals():
    """KipRun writes a jogged recovery as an ACTIVE step, so no REST lap ever appears."""
    assert is_interval_session(LADDER_LAPS)


def test_the_efforts_of_such_a_ladder_carry_no_rest():
    session = sessions_from_activities([_activity(laps=LADDER_LAPS)])[0]
    assert len(session.intervals) == 7
    assert {i.rest_s for i in session.intervals} == {None}


def test_auto_lapped_kilometres_are_never_read_as_efforts():
    """Six 1 km ACTIVE laps are the watch splitting a steady run, not six reps."""
    assert not is_interval_session([_lap("ACTIVE", 1000, 353.1) for _ in range(6)])


def test_too_few_short_efforts_to_be_a_rep_session():
    assert not is_interval_session([_lap("ACTIVE", 400, 95.0) for _ in range(3)])


def test_a_free_run_is_a_single_distance():
    assert not is_interval_session(FREE_LAPS)


def test_garmins_own_interval_flag_does_not_decide():
    """A Zone 2 run carries hasIntensityIntervals=True but is one continuous effort."""
    zone2 = _activity(laps=STEADY_LAPS, hasIntensityIntervals=True)
    assert sessions_from_activities([zone2])[0].model == SINGLE_DISTANCE


def test_an_interval_session_keeps_every_rep_and_its_rest():
    session = sessions_from_activities([_activity(laps=REP_LAPS, km=8.99, secs=3066.6)])[0]
    assert session.model == INTERVALS
    assert len(session.intervals) == 5
    assert session.intervals[0] == CardioInterval(distance_m=500, duration_s=118.0, rest_s=80.4)
    assert session.intervals[-1].rest_s == pytest.approx(89.1)


def test_the_total_stays_the_whole_session_not_just_the_reps():
    """Warm-up and cool-down are volume that was run; BTWB's total time must include it."""
    session = sessions_from_activities([_activity(laps=REP_LAPS, km=8.99, secs=3066.6)])[0]
    assert session.distance_m == pytest.approx(8990)
    assert session.duration_s == pytest.approx(3066.6)


def test_a_rep_without_a_following_rest_lap_records_no_rest():
    laps = [_lap("ACTIVE", 400, 90.0), _lap("REST", 200, 60.0), _lap("ACTIVE", 400, 92.0)]
    session = sessions_from_activities([_activity(laps=laps)])[0]
    assert [i.rest_s for i in session.intervals] == [60.0, None]


def test_uniform_reps_are_the_same_each_interval():
    assert same_each_interval([CardioInterval(500, 118), CardioInterval(497, 120)])


def test_a_ladder_is_not_the_same_each_interval():
    assert not same_each_interval([CardioInterval(100, 20), CardioInterval(400, 95)])


# ── Which activities are synced at all ────────────────────────────────────────


def test_runs_and_rides_map_to_their_btwb_movements():
    sessions = sessions_from_activities(
        [_activity("running"), _activity("cycling", activity_id=2, km=40.5)]
    )
    assert {s.movement for s in sessions} == {MOVEMENT_RUN, MOVEMENT_ROAD_BIKE}


def test_a_track_run_is_still_a_run():
    assert sessions_from_activities([_activity("track_running")])[0].movement == MOVEMENT_RUN


def test_walking_and_crossfit_are_not_synced():
    """They are either not this kind of training or already logged in BTWB by hand."""
    assert sessions_from_activities([_activity("walking"), _activity("hiit", activity_id=2)]) == []


def test_only_runs_are_worth_a_lap_request():
    assert needs_laps(_activity("running"))
    assert not needs_laps(_activity("cycling"))


# ── Commutes ──────────────────────────────────────────────────────────────────


def _commute(activity_id, km, secs, start, **kw):
    return _activity(
        "cycling", activity_id=activity_id, name="Cyclisme", km=km, secs=secs, start=start, **kw
    )


def test_a_days_short_rides_become_one_entry():
    sessions = sessions_from_activities(
        [
            _commute(1, 4.93, 732, "2026-09-08 16:27:51"),
            _commute(2, 5.05, 1179, "2026-09-08 18:16:48"),
        ],
        min_bike_km=10,
    )
    assert len(sessions) == 1
    merged = sessions[0]
    assert merged.title == COMMUTE_TITLE
    assert merged.distance_m == pytest.approx(9980)
    assert merged.duration_s == pytest.approx(1911)
    assert merged.source_ids == (1, 2)


def test_short_rides_on_different_days_stay_apart():
    sessions = sessions_from_activities(
        [
            _commute(1, 4.93, 732, "2026-09-08 16:27:51"),
            _commute(2, 5.05, 1179, "2026-09-10 18:16:48"),
        ],
        min_bike_km=10,
    )
    assert [s.date for s in sessions] == [date(2026, 9, 8), date(2026, 9, 10)]


def test_a_real_ride_keeps_its_own_entry_beside_the_commutes():
    sessions = sessions_from_activities(
        [
            _commute(1, 4.93, 732, "2026-09-06 08:00:00"),
            _activity(
                "cycling",
                activity_id=2,
                name="Ste-Catherine",
                km=40.53,
                secs=5425,
                start="2026-09-06 09:00:00",
            ),
        ],
        min_bike_km=10,
    )
    assert [s.title for s in sessions] == ["Bike Commute", "Ste-Catherine"]
    assert sessions[1].distance_m == pytest.approx(40530)


def test_the_merge_averages_heart_rate_by_time_on_the_bike():
    sessions = sessions_from_activities(
        [
            _commute(1, 5, 600, "2026-09-08 16:00:00", avg_hr=100, max_hr=120),
            _commute(2, 5, 1800, "2026-09-08 18:00:00", avg_hr=140, max_hr=160),
        ],
        min_bike_km=10,
    )
    assert "Avg HR 130 bpm · Max 160 bpm" in sessions[0].notes


def test_the_threshold_is_configurable():
    rides = [_commute(1, 8.0, 1200, "2026-09-08 16:00:00")]
    assert sessions_from_activities(rides, min_bike_km=5)[0].title == "Cyclisme"
    assert sessions_from_activities(rides, min_bike_km=10)[0].title == COMMUTE_TITLE


# ── Notes ─────────────────────────────────────────────────────────────────────


def test_a_run_carries_pace_heart_rate_elevation_and_its_garmin_link():
    session = sessions_from_activities(
        [_activity(activity_id=23865416880, km=10.0, secs=3600, avg_hr=139, max_hr=178, gain=95)]
    )[0]
    assert "Avg HR 139 bpm · Max 178 bpm" in session.notes
    assert "Avg pace 6:00 /km" in session.notes
    assert "Elevation gain 95 m" in session.notes
    assert "https://connect.garmin.com/modern/activity/23865416880" in session.notes


def test_a_ride_is_described_in_speed_not_pace():
    session = sessions_from_activities([_activity("cycling", km=40.0, secs=7200)], min_bike_km=10)[
        0
    ]
    assert "Avg speed 20.0 km/h" in session.notes
    assert "pace" not in session.notes


def test_the_notes_lead_with_the_name_the_session_was_prescribed_under():
    """BTWB names the entry from the movement, so "5 x 500m (R=200m)" lives or dies here."""
    session = sessions_from_activities([_activity(name="La Prairie - 5 x 500m (R=200m)")])[0]
    assert session.notes.startswith("La Prairie - 5 x 500m (R=200m)\n")


def test_a_session_without_heart_rate_says_nothing_about_it():
    assert "HR" not in sessions_from_activities([_activity()])[0].notes


@pytest.mark.parametrize(
    ("seconds", "expected"), [(0, "0:00"), (67, "1:07"), (3067, "51:07"), (3661, "1:01:01")]
)
def test_durations_read_as_a_training_log_writes_them(seconds, expected):
    assert clock(seconds) == expected


def test_commutes_can_be_left_out_of_btwb_entirely():
    """A backfill of training only: months of travel would bury what was trained."""
    sessions = sessions_from_activities(
        [
            _commute(1, 4.93, 732, "2026-09-08 16:27:51"),
            _commute(2, 5.05, 1179, "2026-09-08 18:16:48"),
            _activity(
                "cycling",
                activity_id=3,
                name="Ste-Catherine",
                km=28.1,
                secs=3808,
                start="2026-09-08 09:00:00",
            ),
        ],
        min_bike_km=10,
        merge_commutes=False,
    )
    assert [s.title for s in sessions] == ["Ste-Catherine"]


def test_runs_are_never_treated_as_commutes():
    sessions = sessions_from_activities([_activity(km=1.5)], min_bike_km=10, merge_commutes=False)
    assert len(sessions) == 1
