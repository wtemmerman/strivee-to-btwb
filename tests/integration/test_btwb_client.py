"""Integration tests for the BTWB Playwright client.

These tests require valid BTWB credentials in .env and a live network connection.
They are skipped automatically when credentials are missing.
"""

from datetime import date

import pytest

from strivee_btwb import config

pytestmark = pytest.mark.skipif(
    not config.BTWB_EMAIL or not config.BTWB_PASSWORD,
    reason="BTWB_EMAIL / BTWB_PASSWORD not configured — skipping BTWB integration tests",
)


def _make_week():
    from strivee_btwb.models import DayProgramming, ProgrammingBlock, WeeklyProgramming

    return WeeklyProgramming(
        week_start=date(2026, 4, 27),
        days=[
            DayProgramming(
                date=date(2026, 4, 27),
                day_label="Mon",
                blocks=[
                    ProgrammingBlock(
                        name="Integration Test Block",
                        content="3x5 Back Squat @ 70%\nRest 3 min",
                    )
                ],
            )
        ],
    )


def test_post_week_dry_run_returns_results():
    from strivee_btwb.btwb_client import post_week

    week = _make_week()
    results = post_week(
        week=week,
        email=config.BTWB_EMAIL,
        password=config.BTWB_PASSWORD,
        dry_run=True,
    )
    assert len(results) == 1
    assert results[0]["dry_run"] is True
    assert results[0]["block"] == "Integration Test Block"


def test_authentication_error_on_bad_credentials():
    from strivee_btwb.btwb_client import AuthenticationError, post_week

    week = _make_week()
    with pytest.raises(AuthenticationError):
        post_week(
            week=week,
            email="bad@example.com",
            password="wrongpassword",
            dry_run=False,
            headless=True,
        )
