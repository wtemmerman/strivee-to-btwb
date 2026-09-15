"""Unit tests for the Garmin sync step: its window, its cache rule, and its dedupe."""

from datetime import date, timedelta

import pytest

from strivee_btwb import pipeline
from strivee_btwb.btwb import cardio
from strivee_btwb.core.models import SINGLE_DISTANCE, CardioSession


def _activity(activity_id, day, km=10.0):
    return {
        "activityId": activity_id,
        "activityName": f"Run {activity_id}",
        "activityType": {"typeKey": "running"},
        "startTimeLocal": f"{day} 08:00:00",
        "distance": km * 1000,
        "duration": 3600.0,
    }


# ── The window ────────────────────────────────────────────────────────────────


def test_a_named_week_syncs_monday_to_sunday():
    assert pipeline._garmin_window(7, date(2026, 9, 7)) == (date(2026, 9, 7), date(2026, 9, 13))


def test_without_a_week_the_window_ends_today():
    start, end = pipeline._garmin_window(7, None)
    assert end == date.today()
    assert (end - start).days == 6


def test_a_window_inside_one_week_asks_for_that_week_only():
    assert pipeline._mondays_between(date(2026, 9, 8), date(2026, 9, 11)) == [date(2026, 9, 7)]


def test_a_window_spanning_a_monday_asks_for_both_weeks():
    assert pipeline._mondays_between(date(2026, 9, 3), date(2026, 9, 9)) == [
        date(2026, 8, 31),
        date(2026, 9, 7),
    ]


# ── The cache rule ────────────────────────────────────────────────────────────


@pytest.fixture
def _garmin_calls(monkeypatch):
    calls: dict[str, list] = {"fetched": [], "saved": []}

    def fetch(_client, start, end, _needs_laps):
        calls["fetched"].append(start)
        return [_activity(1, start.isoformat())]

    monkeypatch.setattr(pipeline, "garmin_connect", lambda: "client")
    monkeypatch.setattr(pipeline, "fetch_with_laps", fetch)
    monkeypatch.setattr(pipeline, "save_garmin_week", lambda ws, acts: calls["saved"].append(ws))
    return calls


def test_a_finished_week_is_read_from_cache(monkeypatch, _garmin_calls):
    finished = pipeline.week_start(date.today()) - timedelta(weeks=2)
    monkeypatch.setattr(
        pipeline, "load_garmin_week", lambda ws: [_activity(9, (finished).isoformat())]
    )
    activities = pipeline._garmin_activities(finished, finished + timedelta(days=6), refetch=False)
    assert [a["activityId"] for a in activities] == [9]
    assert _garmin_calls["fetched"] == []


def test_the_current_week_is_always_asked_for_again(monkeypatch, _garmin_calls):
    """A file written on Wednesday cannot know about Friday's run."""
    this_week = pipeline.week_start(date.today())
    monkeypatch.setattr(pipeline, "load_garmin_week", lambda ws: [_activity(9, str(this_week))])
    pipeline._garmin_activities(this_week, date.today(), refetch=False)
    assert _garmin_calls["fetched"] == [this_week]


def test_refetch_ignores_a_finished_weeks_cache(monkeypatch, _garmin_calls):
    finished = pipeline.week_start(date.today()) - timedelta(weeks=2)
    monkeypatch.setattr(pipeline, "load_garmin_week", lambda ws: [_activity(9, str(finished))])
    pipeline._garmin_activities(finished, finished + timedelta(days=6), refetch=True)
    assert _garmin_calls["fetched"] == [finished]


def test_activities_outside_the_window_are_dropped(monkeypatch, _garmin_calls):
    week = pipeline.week_start(date.today()) - timedelta(weeks=2)
    cached = [_activity(1, str(week)), _activity(2, str(week + timedelta(days=5)))]
    monkeypatch.setattr(pipeline, "load_garmin_week", lambda ws: cached)
    kept = pipeline._garmin_activities(week, week + timedelta(days=2), refetch=False)
    assert [a["activityId"] for a in kept] == [1]


# ── Dedupe against BTWB ───────────────────────────────────────────────────────


def _session(*source_ids):
    return CardioSession(
        date=date(2026, 9, 8),
        movement="Road Bike",
        model=SINGLE_DISTANCE,
        title="Bike Commute",
        distance_m=9980,
        duration_s=1911,
        source_ids=source_ids,
    )


def test_a_session_btwb_already_holds_is_recognised():
    assert cardio._already_synced(_session(1, 2), {2: "https://btwb/x"}) == "https://btwb/x"


def test_an_unlogged_session_is_not_recognised():
    assert cardio._already_synced(_session(1, 2), {99: "https://btwb/x"}) is None


def test_a_partly_logged_merge_is_left_alone(caplog):
    """A day that gained a ride after it was logged must not be posted a second time."""
    with caplog.at_level("WARNING"):
        assert cardio._already_synced(_session(1, 2, 3), {1: "https://btwb/x"})
    assert "holds only 1" in caplog.text


def test_the_date_btwb_prints_is_read_back():
    assert cardio._listed_date("September 13, 2026") == date(2026, 9, 13)


def test_an_unreadable_date_says_what_to_check():
    with pytest.raises(cardio.BTWBError, match="still set to English"):
        cardio._listed_date("13 septembre 2026")
