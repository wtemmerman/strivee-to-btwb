"""Unit tests for the Garmin client's rate-limit handling and token requirement."""

import pytest
from garminconnect import GarminConnectTooManyRequestsError

from strivee_btwb.core import config as cfg
from strivee_btwb.garmin import client as gc


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(gc.time, "sleep", lambda _: None)


def _rate_limited(times: int, result="ok"):
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        if calls["n"] <= times:
            raise GarminConnectTooManyRequestsError("429")
        return result

    call.calls = calls
    return call


def test_a_rate_limited_call_is_retried_until_it_answers():
    call = _rate_limited(2)
    assert gc._with_backoff(call, "activities") == "ok"
    assert call.calls["n"] == 3


def test_a_call_that_stays_rate_limited_fails_loudly():
    """An empty week returned silently would look exactly like a week with no training."""
    call = _rate_limited(99)
    with pytest.raises(GarminConnectTooManyRequestsError):
        gc._with_backoff(call, "activities")
    assert call.calls["n"] == len(gc._RETRY_WAITS) + 1


def test_connecting_without_tokens_says_how_to_get_them(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "GARMIN_TOKENSTORE", str(tmp_path / "absent"))
    with pytest.raises(gc.GarminAuthError, match="garmin-login"):
        gc.connect()


def test_laps_are_fetched_only_for_the_activities_that_need_them(monkeypatch):
    activities = [
        {"activityId": 1, "activityType": {"typeKey": "running"}},
        {"activityId": 2, "activityType": {"typeKey": "cycling"}},
    ]
    monkeypatch.setattr(gc, "fetch_activities", lambda *_: activities)
    monkeypatch.setattr(gc, "fetch_laps", lambda _client, activity_id: [{"lap": activity_id}])

    enriched = gc.fetch_with_laps(
        None, None, None, lambda a: a["activityType"]["typeKey"] == "running"
    )
    assert enriched[0]["lapDTOs"] == [{"lap": 1}]
    assert "lapDTOs" not in enriched[1]
