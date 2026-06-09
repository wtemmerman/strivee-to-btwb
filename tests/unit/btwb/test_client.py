"""Unit tests for BTWB client — no live browser or network required."""

import logging
from datetime import date
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from strivee_btwb.btwb import client, delete_week, post_week
from strivee_btwb.btwb.client import (
    AuthenticationError,
    BTWBError,
    _add_instruction,
    _blocks_to_post,
    _calendar_month_url,
    _calendar_week_url,
    _collect_deletable_events,
    _delete_event,
    _ensure_track_selected,
    _fetch_existing_block_names,
    _fetch_existing_titles_for_week,
    _fill_and_plan,
    _group_dates_by_month,
    _login,
    _navigate_to_new_workout,
    _post_day,
    _settle_calendar,
)
from strivee_btwb.core.models import DayProgramming, ProgrammingBlock, WeeklyProgramming

WEEK_START = date(2026, 4, 27)


# ── pure helpers (no browser) ─────────────────────────────────────────────────


def test_calendar_week_url_drops_zero_padding():
    assert (
        _calendar_week_url("2026-04-27")
        == "https://beyondthewhiteboard.com/plan/calendar/week/2026/4/27"
    )


def test_calendar_week_url_single_digit_month_and_day():
    assert _calendar_week_url("2026-01-05").endswith("/2026/1/5")


def test_calendar_month_url_drops_zero_padding_and_appends_day_one():
    # The month view needs a day segment (always 1); /month alone is current month.
    assert (
        _calendar_month_url("2026-06-08")
        == "https://beyondthewhiteboard.com/plan/calendar/month/2026/6/1"
    )


def test_group_dates_by_month_single_month():
    grouped = _group_dates_by_month(["2026-06-08", "2026-06-09", "2026-06-10"])
    assert grouped == {(2026, 6): ["2026-06-08", "2026-06-09", "2026-06-10"]}


def test_group_dates_by_month_splits_week_across_months():
    # A week straddling June/July must scan both month views.
    grouped = _group_dates_by_month(["2026-06-29", "2026-06-30", "2026-07-01"])
    assert grouped == {(2026, 6): ["2026-06-29", "2026-06-30"], (2026, 7): ["2026-07-01"]}


def test_blocks_to_post_filters_existing_by_title():
    day = DayProgramming(
        date=date(2026, 4, 27),
        day_label="Mon",
        blocks=[
            ProgrammingBlock(name="Back Squat", content="5x5"),
            ProgrammingBlock(name="WOD", content="21-15-9"),
        ],
    )
    remaining = _blocks_to_post(day, {"Back Squat"})
    assert [b.name for b in remaining] == ["WOD"]


def test_blocks_to_post_empty_when_all_present():
    day = DayProgramming(
        date=date(2026, 4, 27),
        day_label="Mon",
        blocks=[ProgrammingBlock(name="WOD", content="x")],
    )
    assert _blocks_to_post(day, {"WOD"}) == []


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
    # The week-scan JS returns {date: [titles]}.
    page.evaluate.return_value = {"2026-04-27": ["Back Squat", "WOD"]}
    result = _fetch_existing_block_names(page, "2026-04-27")
    assert result == {"Back Squat", "WOD"}
    page.goto.assert_called_once()


def test_fetch_existing_block_names_empty(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_TRACK_ID", "")
    page = MagicMock()
    page.evaluate.return_value = {"2026-04-28": []}
    result = _fetch_existing_block_names(page, "2026-04-28")
    assert result == set()


def test_fetch_existing_block_names_with_track_id(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_TRACK_ID", "42")
    page = MagicMock()
    page.evaluate.return_value = {"2026-04-27": ["WOD"]}
    track_cb = MagicMock()
    track_cb.count.return_value = 1
    track_cb.is_checked.return_value = False
    page.locator.return_value = track_cb
    result = _fetch_existing_block_names(page, "2026-04-27")
    assert "WOD" in result
    track_cb.click.assert_called_once()


def test_fetch_existing_titles_for_week_one_load_many_dates(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_TRACK_ID", "")
    page = MagicMock()
    page.evaluate.return_value = {"2026-04-27": ["Back Squat"], "2026-04-28": ["WOD", "Row"]}
    result = _fetch_existing_titles_for_week(page, ["2026-04-27", "2026-04-28"])
    assert result == {"2026-04-27": {"Back Squat"}, "2026-04-28": {"WOD", "Row"}}
    page.goto.assert_called_once()  # ONE calendar load for the whole week


def test_fetch_existing_titles_for_week_empty_dates_no_load():
    page = MagicMock()
    assert _fetch_existing_titles_for_week(page, []) == {}
    page.goto.assert_not_called()


def test_settle_calendar_is_non_fatal_on_networkidle_timeout(caplog):
    page = MagicMock()
    page.wait_for_load_state.side_effect = PlaywrightTimeoutError("still loading")
    with caplog.at_level(logging.WARNING, logger="btwb"):
        _settle_calendar(page)  # must not raise
    assert "still loading" in caplog.text.lower()


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
    page.evaluate.return_value = {"2026-04-27": ["Back Squat"]}

    day = DayProgramming(
        date=date(2026, 4, 27),
        day_label="Mon",
        blocks=[ProgrammingBlock(name="Back Squat", content="5x5")],
    )
    results = _post_day(page, day, dry_run=False)
    assert results == []


def test_post_day_uses_precomputed_existing_without_fetching(monkeypatch):
    # When post_week passes the week's existing titles, _post_day must not load
    # the calendar itself.
    page = MagicMock()
    monkeypatch.setattr(
        client, "_fetch_existing_block_names", MagicMock(side_effect=AssertionError("fetched!"))
    )
    day = DayProgramming(
        date=date(2026, 4, 27),
        day_label="Mon",
        blocks=[ProgrammingBlock(name="Back Squat", content="5x5")],
    )
    results = _post_day(page, day, dry_run=False, existing={"Back Squat"})
    assert results == []  # already present → skipped, no fetch


# ── _add_instruction (mocked page) ────────────────────────────────────────────


def test_add_instruction_skips_when_empty():
    page = MagicMock()
    _add_instruction(page, ProgrammingBlock(name="WOD", content="x", instruction=""))
    page.locator.assert_not_called()  # early return, no page interaction


def test_add_instruction_fills_and_saves(caplog):
    page = MagicMock()
    block = ProgrammingBlock(name="Back Squat", content="x", instruction="Stay tight")
    with caplog.at_level(logging.INFO, logger="btwb"):
        _add_instruction(page, block)
    page.wait_for_load_state.assert_called()  # settled after saving the note
    assert "instruction saved" in caplog.text.lower()


def test_add_instruction_handles_timeout(caplog):
    page = MagicMock()
    page.locator.return_value.wait_for.side_effect = PlaywrightTimeoutError("timeout")
    block = ProgrammingBlock(name="WOD", content="x", instruction="note")
    with caplog.at_level(logging.WARNING, logger="btwb"):
        _add_instruction(page, block)  # must not raise
    assert "instruction tab not found" in caplog.text.lower()


# ── _fill_and_plan (mocked page) ──────────────────────────────────────────────


def test_fill_and_plan_runs_full_flow(caplog):
    page = MagicMock()
    # Drive the optional branches: track-select present & unset, title field present.
    page.locator.return_value.count.return_value = 1
    page.locator.return_value.input_value.return_value = ""
    block = ProgrammingBlock(name="EMF 60 : WOD", content="AMRAP 12", instruction="")
    with caplog.at_level(logging.INFO, logger="btwb"):
        _fill_and_plan(page, block, last_block=True)
    page.locator.return_value.fill.assert_called()  # description / title filled
    page.wait_for_load_state.assert_called()  # last_block → networkidle
    assert "saved" in caplog.text.lower()


def test_fill_and_plan_skips_optional_steps_when_absent():
    page = MagicMock()
    page.locator.return_value.count.return_value = 0  # no track select, no title field
    block = ProgrammingBlock(name="WOD", content="x", instruction="")
    _fill_and_plan(page, block, last_block=False)  # must not raise
    page.locator.return_value.select_option.assert_not_called()


# ── _post_day (live posting loop, helpers mocked) ─────────────────────────────


def test_post_day_posts_each_block(monkeypatch):
    page = MagicMock()
    monkeypatch.setattr(client, "_fetch_existing_block_names", lambda *a, **k: set())
    monkeypatch.setattr(client, "_fill_and_plan", lambda *a, **k: None)
    day = DayProgramming(
        date=date(2026, 4, 27),
        day_label="Mon",
        blocks=[ProgrammingBlock(name="A", content="x"), ProgrammingBlock(name="B", content="y")],
    )
    results = _post_day(page, day, dry_run=False)
    assert [r["block"] for r in results] == ["A", "B"]
    assert all(r.get("ok") for r in results)
    page.goto.assert_called()  # first block navigates to the new-workout form


def test_post_day_raises_without_page_when_not_dry_run():
    # The non-dry-run path requires a live page; passing None is a programmer error.
    day = DayProgramming(
        date=date(2026, 4, 27),
        day_label="Mon",
        blocks=[ProgrammingBlock(name="A", content="x")],
    )
    with pytest.raises(BTWBError):
        _post_day(None, day, dry_run=False)


def test_post_day_marks_block_skipped_on_timeout(monkeypatch, caplog):
    page = MagicMock()
    monkeypatch.setattr(client, "_fetch_existing_block_names", lambda *a, **k: set())
    monkeypatch.setattr(
        client, "_fill_and_plan", MagicMock(side_effect=PlaywrightTimeoutError("no preview"))
    )
    day = DayProgramming(
        date=date(2026, 4, 27),
        day_label="Mon",
        blocks=[ProgrammingBlock(name="A", content="x")],
    )
    with caplog.at_level(logging.WARNING, logger="btwb"):
        results = _post_day(page, day, dry_run=False)
    assert results[0]["skipped"] is True
    assert "did not generate a preview" in caplog.text.lower()


# ── _navigate_to_new_workout (mocked page) ────────────────────────────────────


def test_navigate_to_new_workout_direct_url_when_not_via_plus():
    page = MagicMock()
    _navigate_to_new_workout(page, "2026-04-27", via_plus=False)
    url = page.goto.call_args.args[0]
    assert url.endswith("/plan/track_events/workouts/new?d=2026-04-27")
    page.locator.assert_not_called()  # no "+" dropdown interaction


def test_navigate_to_new_workout_uses_plus_dropdown_when_via_plus():
    page = MagicMock()
    page.locator.return_value.get_attribute.return_value = "/plan/track_events/workouts/new?d=x"
    _navigate_to_new_workout(page, "2026-04-27", via_plus=True)
    page.locator.return_value.first.click.assert_called()  # "+" dropdown opened
    page.goto.assert_called_once()  # navigated to the dropdown's href


def test_navigate_to_new_workout_falls_back_to_direct_url_on_missing_plus(caplog):
    page = MagicMock()
    # The "+" button never becomes visible (previous block failed to save).
    page.locator.return_value.first.wait_for.side_effect = PlaywrightTimeoutError("no +")
    with caplog.at_level(logging.WARNING, logger="btwb"):
        _navigate_to_new_workout(page, "2026-04-27", via_plus=True)
    url = page.goto.call_args.args[0]
    assert url.endswith("/plan/track_events/workouts/new?d=2026-04-27")
    assert "falling back" in caplog.text.lower()


# ── _post_day recovery: a failed block must not strand later blocks ────────────


def test_post_day_recovers_to_next_block_after_failure(monkeypatch, caplog):
    """First block's AI fails; the second block must still be posted via the
    direct new-workout URL rather than the (absent) "+" dropdown."""
    page = MagicMock()
    monkeypatch.setattr(client, "_fetch_existing_block_names", lambda *a, **k: set())

    calls: list[bool] = []

    def fake_navigate(_page, _date, via_plus):
        calls.append(via_plus)

    monkeypatch.setattr(client, "_navigate_to_new_workout", fake_navigate)

    # Block A fails (AI produced no preview); block B succeeds.
    fill = MagicMock(side_effect=[PlaywrightTimeoutError("no preview"), None])
    monkeypatch.setattr(client, "_fill_and_plan", fill)

    day = DayProgramming(
        date=date(2026, 4, 27),
        day_label="Mon",
        blocks=[ProgrammingBlock(name="A", content="x"), ProgrammingBlock(name="B", content="y")],
    )
    with caplog.at_level(logging.WARNING, logger="btwb"):
        results = _post_day(page, day, dry_run=False)

    assert results[0]["skipped"] is True
    assert results[1]["ok"] is True
    # Both blocks navigated directly (via_plus=False): the first because it's
    # first, the second because its predecessor failed and left no "+".
    assert calls == [False, False]


def test_post_day_uses_plus_for_block_after_success(monkeypatch):
    """When a block saves, the next one is started from the "+" dropdown."""
    page = MagicMock()
    monkeypatch.setattr(client, "_fetch_existing_block_names", lambda *a, **k: set())
    monkeypatch.setattr(client, "_fill_and_plan", lambda *a, **k: None)

    calls: list[bool] = []
    monkeypatch.setattr(
        client,
        "_navigate_to_new_workout",
        lambda _page, _date, via_plus: calls.append(via_plus),
    )

    day = DayProgramming(
        date=date(2026, 4, 27),
        day_label="Mon",
        blocks=[ProgrammingBlock(name="A", content="x"), ProgrammingBlock(name="B", content="y")],
    )
    _post_day(page, day, dry_run=False)
    assert calls == [False, True]


# ── deletion: _collect_deletable_events / _delete_event (mocked page) ─────────


def _deletable(date_str="2026-06-08", te_id="317667202", title="EMF 60 : WOD"):
    return {
        "date": date_str,
        "id": te_id,
        "action": f"/plan/track_events/{te_id}",
        "token": "csrf-token",
        "title": title,
    }


def test_collect_deletable_events_scans_each_month_once(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_TRACK_ID", "")
    page = MagicMock()
    page.evaluate.side_effect = [[_deletable()], []]  # June has one, July has none
    events = _collect_deletable_events(page, ["2026-06-29", "2026-07-01"])
    assert len(events) == 1
    assert page.evaluate.call_count == 2  # one scan per month spanned
    assert page.goto.call_count == 2


def test_collect_deletable_events_selects_track(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_TRACK_ID", "156552")
    page = MagicMock()
    track_cb = MagicMock()
    track_cb.is_checked.return_value = False
    page.locator.return_value = track_cb
    page.evaluate.return_value = []
    _collect_deletable_events(page, ["2026-06-08"])
    page.locator.assert_any_call("#plan_track_156552")
    track_cb.click.assert_called_once()  # ticked so the track's workouts render


# ── _ensure_track_selected (mocked page) ──────────────────────────────────────


def test_ensure_track_selected_skips_when_no_track_id(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_TRACK_ID", "")
    page = MagicMock()
    _ensure_track_selected(page)
    page.locator.assert_not_called()


def test_ensure_track_selected_clicks_when_unchecked(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_TRACK_ID", "156552")
    page = MagicMock()
    track_cb = MagicMock()
    track_cb.is_checked.return_value = False
    page.locator.return_value = track_cb
    _ensure_track_selected(page)
    track_cb.click.assert_called_once()


def test_ensure_track_selected_leaves_checked_track_alone(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_TRACK_ID", "156552")
    page = MagicMock()
    track_cb = MagicMock()
    track_cb.is_checked.return_value = True
    page.locator.return_value = track_cb
    _ensure_track_selected(page)
    track_cb.click.assert_not_called()


def test_delete_event_posts_rails_delete_with_token():
    page = MagicMock()
    page.request.post.return_value.status = 200
    assert _delete_event(page, _deletable()) is True
    url, kwargs = page.request.post.call_args.args, page.request.post.call_args.kwargs
    assert url[0] == "https://beyondthewhiteboard.com/plan/track_events/317667202"
    assert kwargs["form"] == {"_method": "delete", "authenticity_token": "csrf-token"}


def test_delete_event_returns_false_on_http_error(caplog):
    page = MagicMock()
    page.request.post.return_value.status = 422
    with caplog.at_level(logging.WARNING, logger="btwb"):
        assert _delete_event(page, _deletable()) is False
    assert "failed to delete" in caplog.text.lower()


# ── delete_week (live browser path, Playwright mocked) ────────────────────────


def _patch_playwright_with_page(monkeypatch, page: MagicMock) -> None:
    """Wire sync_playwright()/login so delete_week runs against a mocked page."""
    pw = MagicMock()
    pw.__enter__.return_value.chromium.launch.return_value.new_context.return_value.new_page.return_value = page  # noqa: E501
    monkeypatch.setattr(client, "sync_playwright", lambda: pw)
    monkeypatch.setattr(client, "_login", MagicMock())


def test_delete_week_deletes_each_found_event(monkeypatch):
    page = MagicMock()
    page.request.post.return_value.status = 200
    _patch_playwright_with_page(monkeypatch, page)
    monkeypatch.setattr(
        client,
        "_collect_deletable_events",
        lambda *a, **k: [_deletable(te_id="1", title="A"), _deletable(te_id="2", title="B")],
    )

    results = delete_week("e@x.com", "pw", ["2026-06-08"], headless=True)
    assert [r["id"] for r in results] == ["1", "2"]
    assert all(r["ok"] for r in results)
    assert page.request.post.call_count == 2


def test_delete_week_dry_run_does_not_delete(monkeypatch):
    page = MagicMock()
    _patch_playwright_with_page(monkeypatch, page)
    monkeypatch.setattr(client, "_collect_deletable_events", lambda *a, **k: [_deletable()])

    results = delete_week("e@x.com", "pw", ["2026-06-08"], dry_run=True, headless=True)
    assert results and all(r["dry_run"] for r in results)
    page.request.post.assert_not_called()


def test_delete_week_returns_empty_when_nothing_found(monkeypatch, caplog):
    page = MagicMock()
    _patch_playwright_with_page(monkeypatch, page)
    monkeypatch.setattr(client, "_collect_deletable_events", lambda *a, **k: [])

    with caplog.at_level(logging.INFO, logger="btwb"):
        results = delete_week("e@x.com", "pw", ["2026-06-08"], headless=True)
    assert results == []
    assert "no deletable workouts" in caplog.text.lower()
    page.request.post.assert_not_called()


def test_delete_week_confirm_declined_deletes_nothing(monkeypatch, caplog):
    page = MagicMock()
    _patch_playwright_with_page(monkeypatch, page)
    monkeypatch.setattr(client, "_collect_deletable_events", lambda *a, **k: [_deletable()])

    with caplog.at_level(logging.INFO, logger="btwb"):
        results = delete_week(
            "e@x.com", "pw", ["2026-06-08"], headless=True, confirm=lambda _events: False
        )
    assert results == []
    assert "cancelled" in caplog.text.lower()
    page.request.post.assert_not_called()


def test_delete_week_confirm_accepted_deletes(monkeypatch):
    page = MagicMock()
    page.request.post.return_value.status = 200
    _patch_playwright_with_page(monkeypatch, page)
    monkeypatch.setattr(client, "_collect_deletable_events", lambda *a, **k: [_deletable()])

    seen: list[list[dict]] = []

    def confirm(events):
        seen.append(events)
        return True

    results = delete_week("e@x.com", "pw", ["2026-06-08"], headless=True, confirm=confirm)
    assert results[0]["ok"] is True
    assert seen and seen[0][0]["id"] == "317667202"  # confirm saw the scanned events
    page.request.post.assert_called_once()


# ── post_week (live browser path, Playwright mocked) ──────────────────────────


def test_post_week_live_path_logs_in_and_posts(monkeypatch):
    monkeypatch.setattr(client, "_login", MagicMock())
    monkeypatch.setattr(client, "_post_day", MagicMock(return_value=[{"block": "A", "ok": True}]))
    monkeypatch.setattr(client, "sync_playwright", lambda: MagicMock())

    week = _make_week(blocks=[ProgrammingBlock(name="A", content="x")])
    results = post_week(week=week, email="e@x.com", password="pw", headless=True)

    assert results == [{"block": "A", "ok": True}]
    client._login.assert_called_once()
    client._post_day.assert_called_once()
