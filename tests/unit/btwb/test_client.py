"""Unit tests for BTWB client — no live browser or network required."""

from datetime import date
from unittest.mock import MagicMock

import pytest

from strivee_btwb.btwb import post_week
from strivee_btwb.btwb.client import (
    AuthenticationError,
    _fetch_existing_block_names,
    _login,
    _post_day,
)
from strivee_btwb.core.models import DayProgramming, ProgrammingBlock, WeeklyProgramming

WEEK_START = date(2026, 4, 27)


def _make_week(blocks: list[ProgrammingBlock] | None = None) -> WeeklyProgramming:
    return WeeklyProgramming(
        week_start=WEEK_START,
        days=[
            DayProgramming(
                date=date(2026, 4, 27),
                day_label="Mon",
                blocks=blocks
                or [
                    ProgrammingBlock(name="Back Squat", content="5x5 @ 80%"),
                    ProgrammingBlock(name="WOD", content="21-15-9\nThrusters"),
                ],
            )
        ],
    )


# ── dry-run path (no browser launched) ───────────────────────────────────────


def test_post_week_dry_run_returns_one_result_per_block():
    week = _make_week()
    results = post_week(week=week, email="x", password="x", dry_run=True)
    assert len(results) == 2
    assert all(r["dry_run"] is True for r in results)


def test_post_week_dry_run_includes_block_name_and_date():
    week = _make_week()
    results = post_week(week=week, email="x", password="x", dry_run=True)
    names = {r["block"] for r in results}
    assert names == {"Back Squat", "WOD"}
    assert all(r["date"] == "2026-04-27" for r in results)


def test_post_week_dry_run_empty_week_returns_no_results():
    week = WeeklyProgramming(week_start=WEEK_START, days=[])
    results = post_week(week=week, email="x", password="x", dry_run=True)
    assert results == []


def test_post_week_dry_run_filters_to_requested_days():
    week = WeeklyProgramming(
        week_start=WEEK_START,
        days=[
            DayProgramming(
                date=date(2026, 4, 27),
                day_label="Mon",
                blocks=[ProgrammingBlock(name="Strength", content="5x5")],
            ),
            DayProgramming(
                date=date(2026, 4, 28),
                day_label="Tue",
                blocks=[ProgrammingBlock(name="WOD", content="AMRAP 20")],
            ),
        ],
    )
    mon_only = [week.days[0]]
    results = post_week(week=week, email="x", password="x", days=mon_only, dry_run=True)
    assert len(results) == 1
    assert results[0]["block"] == "Strength"


# ── _login (mocked Playwright page) ──────────────────────────────────────────


def _make_page(url_after_login: str = "https://beyondthewhiteboard.com/dashboard") -> MagicMock:
    page = MagicMock()
    page.url = url_after_login
    return page


def test_login_success():
    page = _make_page()
    _login(page, "user@example.com", "password")
    page.goto.assert_called_once()
    page.locator.return_value.fill.assert_called()
    page.locator.return_value.first.click.assert_called()
    page.wait_for_url.assert_called_once()


def test_login_raises_on_auth_failure():
    page = _make_page(url_after_login="https://beyondthewhiteboard.com/signin")
    with pytest.raises(AuthenticationError):
        _login(page, "bad@example.com", "wrong")


# ── _fetch_existing_block_names (mocked page) ─────────────────────────────────


def test_fetch_existing_block_names_returns_set(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_TRACK_ID", "")
    page = MagicMock()
    page.evaluate.return_value = ["Back Squat", "WOD"]
    result = _fetch_existing_block_names(page, "2026-04-27")
    assert result == {"Back Squat", "WOD"}
    page.goto.assert_called_once()


def test_fetch_existing_block_names_empty(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_TRACK_ID", "")
    page = MagicMock()
    page.evaluate.return_value = []
    result = _fetch_existing_block_names(page, "2026-04-28")
    assert result == set()


def test_fetch_existing_block_names_with_track_id(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_TRACK_ID", "42")
    page = MagicMock()
    page.evaluate.return_value = ["WOD"]
    track_cb = MagicMock()
    track_cb.count.return_value = 1
    track_cb.is_checked.return_value = False
    page.locator.return_value = track_cb
    result = _fetch_existing_block_names(page, "2026-04-27")
    assert "WOD" in result
    track_cb.click.assert_called_once()


# ── _post_day (dry-run skip path) ─────────────────────────────────────────────


def test_post_day_skips_already_posted_blocks():
    day = DayProgramming(
        date=date(2026, 4, 27),
        day_label="Mon",
        blocks=[ProgrammingBlock(name="Back Squat", content="5x5")],
    )
    results = _post_day(None, day, dry_run=True)
    assert len(results) == 1
    assert results[0]["dry_run"] is True


def test_post_day_returns_empty_when_all_already_posted(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_TRACK_ID", "")
    page = MagicMock()
    page.evaluate.return_value = ["Back Squat"]

    day = DayProgramming(
        date=date(2026, 4, 27),
        day_label="Mon",
        blocks=[ProgrammingBlock(name="Back Squat", content="5x5")],
    )
    results = _post_day(page, day, dry_run=False)
    assert results == []
