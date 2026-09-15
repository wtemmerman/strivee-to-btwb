"""Unit tests for the raw Garmin activity cache."""

from datetime import date

import pytest

from strivee_btwb.core import config as cfg
from strivee_btwb.garmin import cache

WEEK = date(2026, 9, 7)
ACTIVITIES = [{"activityId": 1, "activityName": "La Prairie Course à pied", "distance": 10126.5}]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "GARMIN_DIR", tmp_path / "garmin")


def test_a_fetched_week_round_trips():
    cache.save_week(WEEK, ACTIVITIES)
    assert cache.load_week(WEEK) == ACTIVITIES


def test_a_week_that_was_never_fetched_reads_as_absent():
    assert cache.load_week(WEEK) is None


def test_a_cache_from_an_older_schema_is_ignored_rather_than_mapped():
    path = cache.save_week(WEEK, ACTIVITIES)
    path.write_text(
        path.read_text().replace(f'"schema_version": {cache.SCHEMA_VERSION}', '"schema_version": 0')
    )
    assert cache.load_week(WEEK) is None
